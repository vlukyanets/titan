"""Calendar over the API: events, the week view, busy intervals and preferences."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_accounts_api import MEMBER_PW, OWNER_PW, Api, auth
from titan.api.app import create_app
from titan.domains.accounts.models import Role
from titan.settings import Settings

pytestmark = pytest.mark.db


@pytest.fixture
async def api(db_url: str) -> AsyncIterator[Api]:
    app = create_app(Settings(database_url=db_url))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield Api(client, app)
    await app.state.engine.dispose()


async def test_a_week_view_in_the_owners_zone(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    boris_user = await api.create_user("boris", MEMBER_PW)
    anna = auth(await api.token("anna", OWNER_PW))
    boris = auth(await api.token("boris", MEMBER_PW))

    defaults = await api.client.get("/api/v1/calendar/prefs", headers=anna)
    assert defaults.json() == {
        "time_zone": "UTC",
        "work_start": "09:00:00",
        "work_end": "17:00:00",
        "work_days": [1, 2, 3, 4, 5],
        "buffer_minutes": 10,
        "default_reminder_minutes": 15,
    }
    prefs = {**defaults.json(), "time_zone": "Europe/Kyiv", "work_days": [5, 1, 1]}
    saved = await api.client.put("/api/v1/calendar/prefs", headers=anna, json=prefs)
    assert saved.status_code == 200, saved.text
    assert saved.json()["work_days"] == [1, 5]

    created = await api.client.post(
        "/api/v1/calendar/events",
        headers=anna,
        json={
            "title": "Swimming",
            "starts_at": "2026-10-20T19:00:00+03:00",
            "ends_at": "2026-10-20T20:00:00+03:00",
            "recurrence": "FREQ=WEEKLY;BYDAY=TU",
            "attendees": [str(boris_user.id)],
        },
    )
    assert created.status_code == 201, created.text
    event = created.json()
    assert (event["time_zone"], event["attendees"]) == ("Europe/Kyiv", [str(boris_user.id)])

    window = {"start": "2026-10-19T00:00:00Z", "end": "2026-11-02T00:00:00Z"}
    week = await api.client.get("/api/v1/calendar/events", headers=boris, params=window)
    assert week.status_code == 200, week.text
    # 19:00 in Kyiv both times, across the change to winter time.
    assert [(o["starts_at"], o["recurring"]) for o in week.json()] == [
        ("2026-10-20T16:00:00Z", True),
        ("2026-10-27T17:00:00Z", True),
    ]
    busy = await api.client.get("/api/v1/calendar/busy", headers=boris, params=window)
    assert busy.json()[0] == {
        "starts_at": "2026-10-20T16:00:00Z",
        "ends_at": "2026-10-20T17:00:00Z",
    }

    refused = await api.client.patch(
        f"/api/v1/calendar/events/{event['id']}", headers=boris, json={"title": "Mine"}
    )
    assert refused.status_code == 403
    renamed = await api.client.patch(
        f"/api/v1/calendar/events/{event['id']}", headers=anna, json={"location": "Pool"}
    )
    assert renamed.json()["location"] == "Pool"
    deleted = await api.client.delete(f"/api/v1/calendar/events/{event['id']}", headers=anna)
    assert deleted.status_code == 204
    gone = await api.client.get(f"/api/v1/calendar/events/{event['id']}", headers=boris)
    assert gone.status_code == 404


async def test_bad_calendar_input_is_refused(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    base = {"title": "x", "starts_at": "2026-10-01T09:00:00Z", "ends_at": "2026-10-01T10:00:00Z"}
    for body in (
        {**base, "ends_at": "2026-10-01T08:00:00Z"},
        {**base, "starts_at": "2026-10-01T09:00:00"},
        {**base, "time_zone": "Nowhere/Land"},
        {**base, "colour": "red"},
    ):
        response = await api.client.post("/api/v1/calendar/events", headers=anna, json=body)
        assert response.status_code == 422, (body, response.text)
        assert response.headers["content-type"] == "application/problem+json"
    event = (await api.client.post("/api/v1/calendar/events", headers=anna, json=base)).json()
    for patch in ({"title": None}, {"starts_at": None}, {"time_zone": "Moon/Base"}):
        response = await api.client.patch(
            f"/api/v1/calendar/events/{event['id']}", headers=anna, json=patch
        )
        assert response.status_code == 422, (patch, response.text)
    too_long = {"start": "2026-01-01T00:00:00Z", "end": "2026-06-01T00:00:00Z"}
    window = await api.client.get("/api/v1/calendar/events", headers=anna, params=too_long)
    assert window.status_code == 422
    naive = {"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00Z"}
    assert (
        await api.client.get("/api/v1/calendar/busy", headers=anna, params=naive)
    ).status_code == 422
    bad_prefs = {
        "time_zone": "UTC",
        "work_start": "18:00",
        "work_end": "09:00",
        "work_days": [1],
        "buffer_minutes": 0,
    }
    assert (
        await api.client.put("/api/v1/calendar/prefs", headers=anna, json=bad_prefs)
    ).status_code == 422
