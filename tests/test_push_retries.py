"""The worker pushes again what no device accepted, with growing gaps."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_accounts_api import OWNER_PW, auth
from tests.test_notifications_api import PUSH, FakePusher, PushApi
from titan.api.app import create_app
from titan.domains.accounts.models import Role
from titan.domains.notifications.models import Notification, NotificationKind
from titan.domains.notifications.service import RETRY_DELAYS, NotificationsService
from titan.notify import PushResult
from titan.scheduler.worker import Worker
from titan.settings import Settings

pytestmark = pytest.mark.db


@pytest.fixture
async def api(db_url: str) -> AsyncIterator[PushApi]:
    app = create_app(Settings(database_url=db_url, push_allowed_origins=(PUSH,)))
    app.state.pusher = FakePusher()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        push_api = PushApi(client, app)
        push_api.pusher = app.state.pusher
        yield push_api
    await app.state.engine.dispose()


async def notify(api: PushApi, user_id: object, title: str) -> Notification:
    async with api.app.state.sessions() as session:
        service = NotificationsService(session, pusher=api.pusher, push_origins=(PUSH,))
        return await service.notify(user_id, NotificationKind.SYSTEM, title)  # type: ignore[arg-type]


async def stored(api: PushApi, notification: Notification) -> Notification:
    async with api.app.state.sessions() as session:
        found: Notification | None = await session.get(Notification, notification.id)
        assert found is not None
        return found


async def test_a_failed_push_is_retried_until_a_device_takes_it(api: PushApi) -> None:
    anna = await api.create_user("anna", OWNER_PW, Role.OWNER)
    token = await api.token("anna", OWNER_PW)
    endpoint = await api.register(token, "anna-phone")
    api.pusher.results[endpoint] = PushResult.FAILED
    before = datetime.now(UTC)
    first = await notify(api, anna.id, "Hello")
    assert first.delivered_at is None
    assert first.next_push_at is not None
    assert first.next_push_at - before >= RETRY_DELAYS[0]

    worker = Worker(
        Settings(database_url="postgresql+psycopg://unused@localhost/unused"),
        api.app.state.sessions,
        api.pusher,
    )
    # Not due yet.
    assert await worker.retry_pushes(before) == 0
    assert len(api.pusher.sent) == 1

    later = first.next_push_at + timedelta(seconds=1)
    assert await worker.retry_pushes(later) == 0
    again = await stored(api, first)
    assert (again.push_retries, again.next_push_at) == (1, later + RETRY_DELAYS[1])

    api.pusher.results[endpoint] = PushResult.DELIVERED
    assert again.next_push_at is not None
    assert await worker.retry_pushes(again.next_push_at) == 1
    done = await stored(api, first)
    assert done.delivered_at is not None
    assert done.next_push_at is None
    assert len(api.pusher.sent) == 3


async def test_retries_stop_after_the_last_gap_or_once_read(api: PushApi) -> None:
    anna = await api.create_user("anna", OWNER_PW, Role.OWNER)
    token = await api.token("anna", OWNER_PW)
    endpoint = await api.register(token, "anna-phone")
    api.pusher.results[endpoint] = PushResult.FAILED
    worker = Worker(
        Settings(database_url="postgresql+psycopg://unused@localhost/unused"),
        api.app.state.sessions,
        api.pusher,
    )

    lost = await notify(api, anna.id, "Nobody listens")
    now = datetime.now(UTC)
    for _ in RETRY_DELAYS:
        now += timedelta(hours=1)
        await worker.retry_pushes(now)
    given_up = await stored(api, lost)
    assert (given_up.push_retries, given_up.next_push_at) == (len(RETRY_DELAYS), None)

    seen = await notify(api, anna.id, "Read in the app")
    read = await api.client.post(f"/api/v1/notifications/{seen.id}/read", headers=auth(token))
    assert read.status_code == 200, read.text
    assert (await stored(api, seen)).next_push_at is None
    sent = len(api.pusher.sent)
    await worker.retry_pushes(now + timedelta(days=1))
    assert len(api.pusher.sent) == sent
