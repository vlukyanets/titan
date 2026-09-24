"""Push registration, delivery, the notification history and ownership."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from tests.test_accounts_api import MEMBER_PW, OWNER_PW, Api, auth
from titan.api.app import create_app
from titan.domains.accounts.models import Role, User
from titan.domains.notifications.models import NotificationKind, PushSubscription
from titan.domains.notifications.service import NotificationsService
from titan.notify import PushResult
from titan.settings import Settings

pytestmark = pytest.mark.db

PUSH = "http://push.test"


class FakePusher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self.results: dict[str, PushResult] = {}

    async def send(self, endpoint: str, body: bytes) -> PushResult:
        self.sent.append((endpoint, body))
        return self.results.get(endpoint, PushResult.DELIVERED)

    def endpoints(self) -> list[str]:
        return sorted(endpoint for endpoint, _ in self.sent)


class PushApi(Api):
    pusher: FakePusher

    async def register(self, token: str, topic: str) -> str:
        endpoint = f"{PUSH}/{topic}?up=1"
        response = await self.client.put(
            "/api/v1/devices/current/push", headers=auth(token), json={"endpoint": endpoint}
        )
        assert response.status_code == 204, response.text
        return endpoint

    async def notify(self, username: str, title: str, body: str = "") -> str:
        async with self.app.state.sessions() as session:
            user = await session.scalar(select(User).where(User.username == username))
            assert user is not None
            service = NotificationsService(session, pusher=self.pusher)
            notification = await service.notify(user.id, NotificationKind.REMINDER, title, body)
            return str(notification.id)

    async def subscriptions(self) -> int:
        async with self.app.state.sessions() as session:
            return int(
                await session.scalar(select(func.count()).select_from(PushSubscription)) or 0
            )


@pytest.fixture
async def api(db_url: str) -> AsyncIterator[PushApi]:
    app = create_app(Settings(database_url=db_url, push_allowed_origins=(PUSH,)))
    app.state.pusher = FakePusher()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        push_api = PushApi(client, app)
        push_api.pusher = app.state.pusher
        yield push_api
    await app.state.engine.dispose()


async def test_test_notification_pushes_only_a_reference(api: PushApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    token = await api.token("anna", OWNER_PW)
    endpoint = await api.register(token, "upPhone")

    response = await api.client.post("/api/v1/notifications/test", headers=auth(token))
    assert response.status_code == 201
    created = response.json()
    assert created["kind"] == "system"
    assert created["delivered_at"] is not None

    [(sent_to, body)] = api.pusher.sent
    assert sent_to == endpoint
    assert json.loads(body) == {"notification_id": created["id"], "kind": "system"}

    fetched = await api.client.get(f"/api/v1/notifications/{created['id']}", headers=auth(token))
    assert fetched.status_code == 200
    assert fetched.json()["title"] == created["title"]


async def test_push_message_never_contains_content(api: PushApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.register(await api.token("anna", OWNER_PW), "upPhone")
    await api.notify("anna", "Take the blue pills", "Dose: 20 mg")
    [(_, body)] = api.pusher.sent
    assert b"pills" not in body
    assert b"20 mg" not in body


async def test_registration_is_limited_to_configured_push_servers(api: PushApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    token = await api.token("anna", OWNER_PW)
    for endpoint in (
        "http://127.0.0.1:5432/",
        "http://push.test.evil/up",
        "https://push.test/up",
        "http://push.test@evil.test/up",
        "file:///etc/passwd",
    ):
        response = await api.client.put(
            "/api/v1/devices/current/push", headers=auth(token), json={"endpoint": endpoint}
        )
        assert response.status_code == 422, endpoint
        assert response.headers["content-type"] == "application/problem+json"
        assert endpoint not in response.text
    assert await api.subscriptions() == 0


async def test_without_configured_push_servers_registration_fails(db_url: str) -> None:
    app = create_app(Settings(database_url=db_url))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        plain = Api(client, app)
        await plain.create_user("anna", OWNER_PW, Role.OWNER)
        response = await client.put(
            "/api/v1/devices/current/push",
            headers=auth(await plain.token("anna", OWNER_PW)),
            json={"endpoint": f"{PUSH}/upPhone"},
        )
        assert response.status_code == 422
        assert "no push servers are configured" in response.json()["detail"]
    await app.state.engine.dispose()


async def test_registering_again_replaces_the_endpoint(api: PushApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    token = await api.token("anna", OWNER_PW)
    await api.register(token, "upOld")
    new = await api.register(token, "upNew")
    await api.register(token, "upNew")
    assert await api.subscriptions() == 1
    await api.notify("anna", "hello")
    assert api.pusher.endpoints() == [new]


async def test_pushes_go_to_every_active_device_of_the_user_only(api: PushApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", MEMBER_PW)
    phone = await api.token("anna", OWNER_PW)
    tablet = await api.token("anna", OWNER_PW)
    old = (await api.pair("anna", OWNER_PW, name="old phone")).json()
    phone_endpoint = await api.register(phone, "upPhone")
    tablet_endpoint = await api.register(tablet, "upTablet")
    await api.register(old["token"], "upOldPhone")
    await api.register(await api.token("boris", MEMBER_PW), "upBoris")
    revoked = await api.client.delete(f"/api/v1/devices/{old['device_id']}", headers=auth(phone))
    assert revoked.status_code == 204

    await api.notify("anna", "hello")
    assert api.pusher.endpoints() == sorted([phone_endpoint, tablet_endpoint])


async def test_disabled_users_get_no_pushes(api: PushApi) -> None:
    user = await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.register(await api.token("anna", OWNER_PW), "upPhone")
    await api.execute(update(User).where(User.id == user.id).values(disabled_at=datetime.now(UTC)))
    await api.notify("anna", "hello")
    assert api.pusher.sent == []


async def test_gone_endpoints_are_forgotten_and_failures_leave_it_undelivered(
    api: PushApi,
) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    token = await api.token("anna", OWNER_PW)
    endpoint = await api.register(token, "upPhone")

    api.pusher.results[endpoint] = PushResult.FAILED
    failed = await api.notify("anna", "first")
    assert await api.subscriptions() == 1

    api.pusher.results[endpoint] = PushResult.GONE
    await api.notify("anna", "second")
    assert await api.subscriptions() == 0

    listed = (await api.client.get("/api/v1/notifications", headers=auth(token))).json()
    assert [(n["title"], n["delivered_at"]) for n in listed] == [("second", None), ("first", None)]
    assert listed[1]["id"] == failed


async def test_unregistering_stops_pushes(api: PushApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    token = await api.token("anna", OWNER_PW)
    await api.register(token, "upPhone")
    response = await api.client.delete("/api/v1/devices/current/push", headers=auth(token))
    assert response.status_code == 204
    await api.notify("anna", "hello")
    assert api.pusher.sent == []


async def test_history_pages_newest_first_and_tracks_read_state(api: PushApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    token = await api.token("anna", OWNER_PW)
    ids = [await api.notify("anna", f"n{i}") for i in range(5)]

    page = (await api.client.get("/api/v1/notifications?limit=2", headers=auth(token))).json()
    assert [n["id"] for n in page] == [ids[4], ids[3]]
    rest = await api.client.get(
        f"/api/v1/notifications?before={page[-1]['id']}", headers=auth(token)
    )
    assert [n["id"] for n in rest.json()] == [ids[2], ids[1], ids[0]]

    read = await api.client.post(f"/api/v1/notifications/{ids[0]}/read", headers=auth(token))
    assert read.status_code == 200
    assert read.json()["read_at"] is not None
    unread = (await api.client.get("/api/v1/notifications?unread=true", headers=auth(token))).json()
    assert [n["id"] for n in unread] == [ids[4], ids[3], ids[2], ids[1]]

    result = await api.client.post("/api/v1/notifications/read-all", headers=auth(token))
    assert result.json() == {"updated": 4}
    unread = (await api.client.get("/api/v1/notifications?unread=true", headers=auth(token))).json()
    assert unread == []


async def test_other_peoples_notifications_are_not_found(api: PushApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", MEMBER_PW)
    owner = await api.token("anna", OWNER_PW)
    member = await api.token("boris", MEMBER_PW)
    borises = await api.notify("boris", "private")

    for method, path in (
        ("GET", f"/api/v1/notifications/{borises}"),
        ("POST", f"/api/v1/notifications/{borises}/read"),
    ):
        response = await api.client.request(method, path, headers=auth(owner))
        assert response.status_code == 404
    assert (await api.client.get("/api/v1/notifications", headers=auth(owner))).json() == []
    assert (
        await api.client.post("/api/v1/notifications/read-all", headers=auth(owner))
    ).json() == {"updated": 0}
    mine = (await api.client.get(f"/api/v1/notifications/{borises}", headers=auth(member))).json()
    assert mine["read_at"] is None


async def test_notifications_need_a_token(api: PushApi) -> None:
    for method, path in (
        ("GET", "/api/v1/notifications"),
        ("POST", "/api/v1/notifications/test"),
        ("PUT", "/api/v1/devices/current/push"),
        ("DELETE", "/api/v1/devices/current/push"),
    ):
        response = await api.client.request(method, path)
        assert response.status_code == 401, path
