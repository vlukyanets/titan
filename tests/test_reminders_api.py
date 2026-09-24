"""Reminders over the API, including the notification's Snooze and Done actions."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_accounts_api import MEMBER_PW, OWNER_PW, Api, auth
from titan.api.app import create_app
from titan.domains.accounts.models import Role
from titan.domains.reminders.service import RemindersService
from titan.settings import Settings

pytestmark = pytest.mark.db


@pytest.fixture
async def api(db_url: str) -> AsyncIterator[Api]:
    app = create_app(Settings(database_url=db_url))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield Api(client, app)
    await app.state.engine.dispose()


async def fire_due(api: Api) -> list[uuid.UUID]:
    """What the scheduler will do: fire everything that is due now."""
    async with api.app.state.sessions() as session:
        reminders = RemindersService(session)
        fired = []
        for reminder_id in await reminders.due():
            if await reminders.fire(reminder_id) is not None:
                fired.append(reminder_id)
        return fired


async def test_a_reminder_fires_and_is_snoozed_from_its_notification(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", MEMBER_PW)
    anna = auth(await api.token("anna", OWNER_PW))
    boris = auth(await api.token("boris", MEMBER_PW))
    soon = datetime.now(UTC) - timedelta(seconds=1)
    created = await api.client.post(
        "/api/v1/reminders",
        headers=anna,
        json={"text": "Take the laundry out", "fire_at": soon.isoformat()},
    )
    assert created.status_code == 201, created.text
    reminder = created.json()
    assert (reminder["status"], reminder["link"], reminder["recurrence"]) == (
        "scheduled",
        None,
        None,
    )
    assert await fire_due(api) == [uuid.UUID(reminder["id"])]

    notes = (await api.client.get("/api/v1/notifications", headers=anna)).json()
    assert [(n["kind"], n["body"]) for n in notes] == [("reminder", "Take the laundry out")]
    assert notes[0]["data"] == {"reminder_id": reminder["id"]}

    snoozed = await api.client.post(
        f"/api/v1/reminders/{reminder['id']}/snooze", headers=anna, json={"minutes": 5}
    )
    assert snoozed.status_code == 200, snoozed.text
    assert snoozed.json()["status"] == "snoozed"
    assert snoozed.json()["notification_id"] == notes[0]["id"]
    default = await api.client.post(f"/api/v1/reminders/{reminder['id']}/snooze", headers=anna)
    assert default.status_code == 200, default.text
    assert await fire_due(api) == []

    done = await api.client.post(f"/api/v1/reminders/{reminder['id']}/dismiss", headers=anna)
    assert done.json()["status"] == "dismissed"
    closed = await api.client.post(f"/api/v1/reminders/{reminder['id']}/snooze", headers=anna)
    assert closed.status_code == 409

    hidden = await api.client.get(f"/api/v1/reminders/{reminder['id']}", headers=boris)
    assert hidden.status_code == 404
    assert (await api.client.get("/api/v1/reminders", headers=boris)).json() == []
    deleted = await api.client.delete(f"/api/v1/reminders/{reminder['id']}", headers=anna)
    assert deleted.status_code == 204


async def test_reminders_are_rescheduled_and_linked(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    task = (await api.client.post("/api/v1/tasks", headers=anna, json={"title": "Taxes"})).json()
    created = await api.client.post(
        "/api/v1/reminders",
        headers=anna,
        json={
            "text": "Start on the taxes",
            "fire_at": "2026-10-01T08:00:00+02:00",
            "recurrence": "FREQ=MONTHLY",
            "link": {"type": "task", "id": task["id"]},
        },
    )
    assert created.status_code == 201, created.text
    reminder = created.json()
    assert reminder["fire_at"] == "2026-10-01T06:00:00Z"
    assert reminder["link"] == {"type": "task", "id": task["id"]}

    moved = await api.client.patch(
        f"/api/v1/reminders/{reminder['id']}",
        headers=anna,
        json={"fire_at": "2026-10-02T06:00:00Z", "recurrence": None},
    )
    assert moved.status_code == 200, moved.text
    assert (moved.json()["occurs_at"], moved.json()["recurrence"]) == (
        "2026-10-02T06:00:00Z",
        None,
    )
    for patch in ({"text": None}, {"fire_at": None}, {"when": "later"}):
        refused = await api.client.patch(
            f"/api/v1/reminders/{reminder['id']}", headers=anna, json=patch
        )
        assert refused.status_code == 422, (patch, refused.text)

    for body in (
        {"text": "x", "fire_at": "2026-10-01T08:00:00"},
        {"text": "x", "fire_at": "2026-10-01T08:00:00Z", "recurrence": "FREQ=DAILY;COUNT=2"},
        {
            "text": "x",
            "fire_at": "2026-10-01T08:00:00Z",
            "link": {"type": "task", "id": str(uuid.uuid4())},
        },
    ):
        refused = await api.client.post("/api/v1/reminders", headers=anna, json=body)
        assert refused.status_code == 422, (body, refused.text)
        assert refused.headers["content-type"] == "application/problem+json"
