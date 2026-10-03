"""Browser sign-in with the session cookie (ADR 0012)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update

from tests.test_accounts_api import MEMBER_PW, Api
from titan.api.app import create_app
from titan.api.session import SignInLimiter, browser_name
from titan.domains.accounts.credentials import TOKEN_PREFIX
from titan.domains.accounts.models import Device, Platform
from titan.domains.notifications.models import Notification
from titan.settings import Settings

FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
SAME_SITE = {"X-Titan-Request": "1", "Origin": "https://titan.example"}
COOKIE = "__Host-TSID"


def test_browser_names() -> None:
    assert browser_name(FIREFOX) == "Firefox on Linux"
    chrome_android = "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/140.0 Mobile"
    assert browser_name(chrome_android) == "Chrome on Android"
    edge = "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/140.0 Safari/537.36 Edg/140.0"
    assert browser_name(edge) == "Edge on Windows"
    assert browser_name(None) == "Browser"


def test_the_sign_in_limit_counts_per_address() -> None:
    limiter = SignInLimiter(limit=2, window=60)
    assert [limiter.allow("a"), limiter.allow("a")] == [True, True]
    assert not limiter.allow("a")
    assert limiter.allow("b")


@pytest.fixture
async def api(db_url: str) -> AsyncIterator[Api]:
    app = create_app(Settings(database_url=db_url, node_name="node-a"))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://titan.example",
        headers={"User-Agent": FIREFOX},
    ) as client:
        yield Api(client, app)
    await app.state.engine.dispose()


async def sign_in(api: Api, password: str = MEMBER_PW, **headers: str) -> Response:
    return await api.client.post(
        "/api/v1/session",
        json={"username": "anna", "password": password},
        headers=SAME_SITE | headers,
    )


async def web_device(api: Api) -> Device:
    async with api.app.state.sessions() as session:
        device: Device = (
            await session.scalars(select(Device).where(Device.platform == Platform.WEB))
        ).one()
        return device


@pytest.mark.db
async def test_sign_in_sets_the_cookie_and_the_cookie_authenticates(api: Api) -> None:
    anna = await api.create_user("anna", MEMBER_PW)
    response = await sign_in(api)
    assert response.status_code == 201, response.text
    assert "token" not in response.text
    assert response.json()["user"]["username"] == "anna"
    cookie = response.headers["set-cookie"]
    for part in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=2592000"):
        assert part in cookie
    assert cookie.startswith(f"{COOKIE}={TOKEN_PREFIX}")

    device = await web_device(api)
    assert (device.name, str(device.id)) == ("Firefox on Linux", response.json()["device_id"])
    async with api.app.state.sessions() as session:
        notice = (await session.scalars(select(Notification))).one()
    assert (notice.user_id, notice.title) == (anna.id, "New sign-in")
    assert notice.body == "Firefox on Linux signed in on node-a."

    me = await api.client.get("/api/v1/me")
    assert (me.status_code, me.json()["username"]) == (200, "anna")


@pytest.mark.db
async def test_sign_in_refuses_cross_site_requests_and_wrong_passwords(api: Api) -> None:
    await api.create_user("anna", MEMBER_PW)
    no_header = await api.client.post(
        "/api/v1/session", json={"username": "anna", "password": MEMBER_PW}
    )
    assert no_header.status_code == 403
    foreign = await sign_in(api, Origin="https://evil.example")
    assert foreign.status_code == 403
    wrong = await sign_in(api, "a-wrong-password-1")
    assert wrong.status_code == 401
    assert "set-cookie" not in wrong.headers


@pytest.mark.db
async def test_cookie_writes_need_the_request_header(api: Api) -> None:
    await api.create_user("anna", MEMBER_PW)
    await sign_in(api)
    note = {"title": "Milk"}
    refused = await api.client.post("/api/v1/notes", json=note)
    assert refused.status_code == 403
    foreign = await api.client.post(
        "/api/v1/notes", json=note, headers=SAME_SITE | {"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    created = await api.client.post("/api/v1/notes", json=note, headers=SAME_SITE)
    assert created.status_code == 201, created.text


@pytest.mark.db
async def test_bearer_tokens_need_no_header_and_device_tokens_are_no_cookies(api: Api) -> None:
    await api.create_user("anna", MEMBER_PW)
    token = await api.token("anna", MEMBER_PW)
    bearer = {"Authorization": f"Bearer {token}"}
    created = await api.client.post("/api/v1/notes", json={"title": "Milk"}, headers=bearer)
    assert created.status_code == 201
    # A paired device's token is not a browser session.
    api.client.cookies.set(COOKIE, token)
    refused = await api.client.get("/api/v1/me")
    assert refused.status_code == 401
    assert refused.headers["set-cookie"].startswith(f'{COOKIE}=""; ')
    assert "Max-Age=0" in refused.headers["set-cookie"]


@pytest.mark.db
async def test_sign_out_revokes_the_device(api: Api) -> None:
    await api.create_user("anna", MEMBER_PW)
    await sign_in(api)
    stolen = api.client.cookies[COOKIE]
    out = await api.client.delete("/api/v1/session", headers=SAME_SITE)
    assert out.status_code == 204
    assert "Max-Age=0" in out.headers["set-cookie"]
    assert (await web_device(api)).revoked_at is not None
    api.client.cookies.set(COOKIE, stolen)
    assert (await api.client.get("/api/v1/me")).status_code == 401


@pytest.mark.db
@pytest.mark.parametrize(
    "aged",
    [
        {"last_seen_at": timedelta(days=31)},
        {"created_at": timedelta(days=91), "last_seen_at": timedelta(minutes=1)},
    ],
)
async def test_idle_and_old_sessions_expire(api: Api, aged: dict[str, timedelta]) -> None:
    await api.create_user("anna", MEMBER_PW)
    await sign_in(api)
    device = await web_device(api)
    now = datetime.now(UTC)
    await api.execute(
        update(Device)
        .where(Device.id == device.id)
        .values({name: now - age for name, age in aged.items()})
    )
    assert (await api.client.get("/api/v1/me")).status_code == 401
    assert (await web_device(api)).revoked_at is not None


@pytest.mark.db
async def test_an_active_session_renews_its_cookie_hourly(api: Api) -> None:
    await api.create_user("anna", MEMBER_PW)
    await sign_in(api)
    quiet = await api.client.get("/api/v1/me")
    assert "set-cookie" not in quiet.headers
    device = await web_device(api)
    await api.execute(
        update(Device)
        .where(Device.id == device.id)
        .values(last_seen_at=datetime.now(UTC) - timedelta(hours=2))
    )
    renewed = await api.client.get("/api/v1/me")
    assert "Max-Age=2592000" in renewed.headers["set-cookie"]
