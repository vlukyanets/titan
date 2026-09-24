"""Pairing, token authentication, revocation, lockout and account management."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from titan.api.app import create_app
from titan.domains.accounts.models import Device, Role, User
from titan.domains.accounts.service import MAX_FAILED_LOGINS, AccountsService
from titan.settings import Settings

pytestmark = pytest.mark.db

OWNER_PW = "owner-password-123"
MEMBER_PW = "member-password-123"


class Api:
    def __init__(self, client: AsyncClient, app: Any) -> None:
        self.client = client
        self.app = app

    async def create_user(self, username: str, password: str, role: Role = Role.MEMBER) -> User:
        async with self.app.state.sessions() as session:
            return await AccountsService(session).create_user(username, password, role=role)

    async def pair(self, username: str, password: str, name: str = "test phone") -> Any:
        return await self.client.post(
            "/api/v1/devices/pair",
            json={
                "username": username,
                "password": password,
                "device_name": name,
                "platform": "android",
            },
        )

    async def token(self, username: str, password: str) -> str:
        response = await self.pair(username, password)
        assert response.status_code == 201, response.text
        return str(response.json()["token"])

    async def execute(self, statement: Any) -> None:
        async with self.app.state.sessions() as session:
            await session.execute(statement)
            await session.commit()

    async def device(self, device_id: str) -> Device:
        async with self.app.state.sessions() as session:
            device: Device | None = await session.get(Device, uuid.UUID(device_id))
            assert device is not None
            return device


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def api(db_url: str) -> AsyncIterator[Api]:
    app = create_app(Settings(database_url=db_url))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield Api(client, app)
    await app.state.engine.dispose()


async def test_pair_then_use_the_token(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    response = await api.pair("Anna", OWNER_PW, name="Pixel 9")
    assert response.status_code == 201
    body = response.json()
    assert body["token"].startswith("tt_")
    assert body["user"]["username"] == "anna"
    assert body["user"]["role"] == "owner"

    me = await api.client.get("/api/v1/me", headers=auth(body["token"]))
    assert me.status_code == 200
    assert me.json()["username"] == "anna"

    devices = (await api.client.get("/api/v1/devices", headers=auth(body["token"]))).json()
    assert [(d["name"], d["platform"], d["current"]) for d in devices] == [
        ("Pixel 9", "android", True)
    ]


async def test_token_is_stored_only_as_a_hash(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    body = (await api.pair("anna", OWNER_PW)).json()
    device = await api.device(body["device_id"])
    assert device.token_hash != body["token"]
    assert body["token"] not in device.token_hash


async def test_unknown_user_and_wrong_password_look_the_same(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    wrong = await api.pair("anna", "not-the-password")
    unknown = await api.pair("nobody", "not-the-password")
    malformed = await api.pair("x", "not-the-password")
    for response in (wrong, unknown, malformed):
        assert response.status_code == 401
        assert response.headers["content-type"] == "application/problem+json"
    assert wrong.json() == unknown.json() == malformed.json()


@pytest.mark.parametrize("header", [None, "Bearer nope", "Bearer tt_forged", "Basic abc"])
async def test_requests_without_a_valid_token_are_rejected(api: Api, header: str | None) -> None:
    headers = {"Authorization": header} if header else {}
    response = await api.client.get("/api/v1/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"] == "application/problem+json"


async def test_revoked_device_token_stops_working(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    phone = await api.token("anna", OWNER_PW)
    laptop = await api.token("anna", OWNER_PW)
    devices = (await api.client.get("/api/v1/devices", headers=auth(laptop))).json()
    phone_id = next(d["id"] for d in devices if not d["current"])

    response = await api.client.delete(f"/api/v1/devices/{phone_id}", headers=auth(laptop))
    assert response.status_code == 204
    assert (await api.client.get("/api/v1/me", headers=auth(phone))).status_code == 401
    assert (await api.client.get("/api/v1/me", headers=auth(laptop))).status_code == 200


async def test_members_cannot_touch_other_peoples_devices(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", MEMBER_PW)
    owner = await api.token("anna", OWNER_PW)
    member_pair = (await api.pair("boris", MEMBER_PW)).json()
    member = member_pair["token"]
    owner_device = (await api.client.get("/api/v1/devices", headers=auth(owner))).json()[0]["id"]

    listed = (await api.client.get("/api/v1/devices", headers=auth(member))).json()
    assert [d["id"] for d in listed] == [member_pair["device_id"]]
    response = await api.client.delete(f"/api/v1/devices/{owner_device}", headers=auth(member))
    assert response.status_code == 404
    assert (await api.client.get("/api/v1/me", headers=auth(owner))).status_code == 200

    # The owner may revoke any device.
    response = await api.client.delete(
        f"/api/v1/devices/{member_pair['device_id']}", headers=auth(owner)
    )
    assert response.status_code == 204
    assert (await api.client.get("/api/v1/me", headers=auth(member))).status_code == 401


async def test_account_locks_after_too_many_failures(api: Api) -> None:
    user = await api.create_user("anna", OWNER_PW, Role.OWNER)
    for _ in range(MAX_FAILED_LOGINS):
        assert (await api.pair("anna", "wrong-password-!!")).status_code == 401
    locked = await api.pair("anna", OWNER_PW)
    assert locked.status_code == 401
    assert locked.json() == (await api.pair("nobody", OWNER_PW)).json()

    await api.execute(
        update(User)
        .where(User.id == user.id)
        .values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert (await api.pair("anna", OWNER_PW)).status_code == 201
    async with api.app.state.sessions() as session:
        fresh = await session.get(User, user.id)
        assert fresh is not None
        assert fresh.failed_logins == 0
        assert fresh.locked_until is None


async def test_successful_login_resets_the_failure_count(api: Api) -> None:
    user = await api.create_user("anna", OWNER_PW, Role.OWNER)
    for _ in range(MAX_FAILED_LOGINS - 1):
        await api.pair("anna", "wrong-password-!!")
    assert (await api.pair("anna", OWNER_PW)).status_code == 201
    await api.pair("anna", "wrong-password-!!")
    assert (await api.pair("anna", OWNER_PW)).status_code == 201
    async with api.app.state.sessions() as session:
        fresh = await session.get(User, user.id)
        assert fresh is not None
        assert fresh.failed_logins == 0


async def test_disabled_user_cannot_pair_or_use_tokens(api: Api) -> None:
    user = await api.create_user("anna", OWNER_PW, Role.OWNER)
    token = await api.token("anna", OWNER_PW)
    await api.execute(update(User).where(User.id == user.id).values(disabled_at=datetime.now(UTC)))
    assert (await api.client.get("/api/v1/me", headers=auth(token))).status_code == 401
    assert (await api.pair("anna", OWNER_PW)).status_code == 401


async def test_last_seen_is_written_at_most_hourly(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    body = (await api.pair("anna", OWNER_PW)).json()
    recent = datetime.now(UTC) - timedelta(minutes=10)
    stale = datetime.now(UTC) - timedelta(hours=2)

    await api.execute(
        update(Device).where(Device.id == body["device_id"]).values(last_seen_at=recent)
    )
    await api.client.get("/api/v1/me", headers=auth(body["token"]))
    assert (await api.device(body["device_id"])).last_seen_at == recent

    await api.execute(
        update(Device).where(Device.id == body["device_id"]).values(last_seen_at=stale)
    )
    await api.client.get("/api/v1/me", headers=auth(body["token"]))
    seen = (await api.device(body["device_id"])).last_seen_at
    assert seen is not None
    assert seen > recent


async def test_owner_manages_accounts(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    owner = await api.token("anna", OWNER_PW)

    created = await api.client.post(
        "/api/v1/users",
        headers=auth(owner),
        json={"username": "Boris", "password": MEMBER_PW, "display_name": "Boris K."},
    )
    assert created.status_code == 201
    assert created.json()["username"] == "boris"
    assert created.json()["role"] == "member"

    duplicate = await api.client.post(
        "/api/v1/users", headers=auth(owner), json={"username": "boris", "password": MEMBER_PW}
    )
    assert duplicate.status_code == 409
    weak = await api.client.post(
        "/api/v1/users", headers=auth(owner), json={"username": "vera", "password": "short"}
    )
    assert weak.status_code == 422

    users = (await api.client.get("/api/v1/users", headers=auth(owner))).json()
    assert [u["username"] for u in users] == ["anna", "boris"]

    member = await api.token("boris", MEMBER_PW)
    assert (await api.client.get("/api/v1/users", headers=auth(member))).status_code == 403
    forbidden = await api.client.post(
        "/api/v1/users", headers=auth(member), json={"username": "vera", "password": MEMBER_PW}
    )
    assert forbidden.status_code == 403


async def test_validation_errors_never_echo_the_password(api: Api) -> None:
    secret = "s3cret-" + "x" * 1100
    response = await api.pair("anna", secret)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "s3cret" not in response.text
    assert response.json()["errors"][0]["loc"] == ["body", "password"]
