"""Trackers, entries and stats over the API."""

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


async def test_templates_are_listed(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    listed = await api.client.get("/api/v1/trackers/templates", headers=anna)
    assert listed.status_code == 200
    mood = next(t for t in listed.json() if t["id"] == "mood")
    assert mood == {
        "id": "mood",
        "kind": "health",
        "unit": "score",
        "min_value": 1.0,
        "max_value": 5.0,
    }
    assert next(t for t in listed.json() if t["id"] == "expense")["unit"] is None


async def test_spending_goes_from_tracker_to_stats(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    created = await api.client.post(
        "/api/v1/trackers",
        headers=anna,
        json={
            "name": "Spending",
            "template": "expense",
            "unit": "EUR",
            "target": {"value": 100, "period": "week", "direction": "at_most"},
        },
    )
    assert created.status_code == 201, created.text
    tracker = created.json()
    assert (tracker["kind"], tracker["min_value"], tracker["archived"]) == ("finance", 0.0, False)
    assert tracker["target"] == {"value": 100.0, "period": "week", "direction": "at_most"}
    base = f"/api/v1/trackers/{tracker['id']}"

    duplicate = await api.client.post(
        "/api/v1/trackers",
        headers=anna,
        json={"name": "spending", "kind": "finance", "unit": "EUR"},
    )
    assert duplicate.status_code == 409

    logged = []
    for value, when, category in (
        (23.4, "2026-10-12T10:00:00Z", "Groceries"),
        (0.1, "2026-10-13T10:00:00Z", None),
        (0.2, "2026-10-13T11:00:00Z", None),
    ):
        response = await api.client.post(
            f"{base}/entries",
            headers=anna,
            json={"value": value, "at": when, "category": category},
        )
        assert response.status_code == 201, response.text
        logged.append(response.json())
    assert logged[0]["category"] == "groceries"
    negative = await api.client.post(f"{base}/entries", headers=anna, json={"value": -1})
    assert negative.status_code == 422

    page = await api.client.get(f"{base}/entries", headers=anna, params={"limit": 2})
    assert [e["id"] for e in page.json()] == [logged[2]["id"], logged[1]["id"]]
    rest = await api.client.get(
        f"{base}/entries", headers=anna, params={"before": page.json()[-1]["id"]}
    )
    assert [e["id"] for e in rest.json()] == [logged[0]["id"]]
    ranged = await api.client.get(
        f"{base}/entries",
        headers=anna,
        params={"from": "2026-10-13T00:00:00Z", "to": "2026-10-13T10:30:00Z"},
    )
    assert [e["id"] for e in ranged.json()] == [logged[1]["id"]]

    stats = await api.client.get(
        f"{base}/stats", headers=anna, params={"from": "2026-10-12", "to": "2026-10-18"}
    )
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert (body["period"], body["time_zone"]) == ("week", "UTC")
    assert (body["start"], body["end"]) == ("2026-10-12", "2026-10-18")
    # Exact decimals: 23.4 + 0.1 + 0.2, not 23.700000000000003.
    assert (body["count"], body["sum"], body["per_period"]) == (3, 23.7, 23.7)
    assert body["buckets"] == [
        {
            "start": "2026-10-12",
            "count": 3,
            "sum": 23.7,
            "average": 7.9,
            "min": 0.1,
            "max": 23.4,
            "met": True,
        }
    ]
    assert body["categories"] == [
        {"category": "groceries", "count": 1, "sum": 23.4},
        {"category": None, "count": 2, "sum": 0.3},
    ]
    assert body["streak_period"] == "week"

    days = await api.client.get(
        f"{base}/stats",
        headers=anna,
        params={"period": "day", "from": "2026-10-12", "to": "2026-10-13"},
    )
    assert [(b["sum"], b["met"]) for b in days.json()["buckets"]] == [(23.4, None), (0.3, None)]
    too_long = await api.client.get(
        f"{base}/stats", headers=anna, params={"period": "day", "from": "2024-01-01"}
    )
    assert too_long.status_code == 422

    patched = await api.client.patch(
        f"{base}/entries/{logged[0]['id']}",
        headers=anna,
        json={"value": 25, "category": None, "note": "and a cake"},
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["value"], patched.json()["category"]) == (25.0, None)
    removed = await api.client.delete(f"{base}/entries/{logged[1]['id']}", headers=anna)
    assert removed.status_code == 204

    archived = await api.client.patch(
        base, headers=anna, json={"archived": True, "target": None, "name": "Old spending"}
    )
    assert archived.status_code == 200, archived.text
    assert (archived.json()["target"], archived.json()["name"]) == (None, "Old spending")
    closed = await api.client.post(f"{base}/entries", headers=anna, json={"value": 1})
    assert closed.status_code == 409
    active = await api.client.get("/api/v1/trackers", headers=anna)
    assert active.json() == []
    old = await api.client.get("/api/v1/trackers", headers=anna, params={"archived": True})
    assert [t["id"] for t in old.json()] == [tracker["id"]]

    deleted = await api.client.delete(base, headers=anna)
    assert deleted.status_code == 204
    gone = await api.client.get(base, headers=anna)
    assert gone.status_code == 404
    assert gone.headers["content-type"] == "application/problem+json"


async def test_a_habit_logged_now_starts_a_streak(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    created = await api.client.post(
        "/api/v1/trackers", headers=anna, json={"name": "Stretching", "template": "habit"}
    )
    base = f"/api/v1/trackers/{created.json()['id']}"
    before = await api.client.get(f"{base}/stats", headers=anna)
    assert (before.json()["streak"], len(before.json()["buckets"])) == (0, 30)
    await api.client.post(f"{base}/entries", headers=anna, json={"value": 1})
    after = await api.client.get(f"{base}/stats", headers=anna)
    assert (after.json()["streak"], after.json()["streak_period"]) == (1, "day")
    assert after.json()["buckets"][-1]["met"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"name": "No kind", "unit": "x"},
        {"name": "Coins", "template": "expense"},
        {"name": "Nope", "template": "steps"},
        {"name": "Odd", "kind": "custom", "unit": "x", "min_value": 2, "max_value": 1},
        {"name": "Bad rule", "template": "habit", "schedule": "FREQ=MINUTELY"},
        {"name": "Extra", "template": "habit", "owner_id": "x"},
        {"name": "", "template": "habit"},
    ],
)
async def test_invalid_trackers_are_refused(api: Api, body: dict[str, object]) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    response = await api.client.post("/api/v1/trackers", headers=anna, json=body)
    assert response.status_code == 422, response.text


async def test_other_users_trackers_do_not_exist_for_you(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", MEMBER_PW, Role.MEMBER)
    anna = auth(await api.token("anna", OWNER_PW))
    boris = auth(await api.token("boris", MEMBER_PW))
    created = await api.client.post(
        "/api/v1/trackers", headers=anna, json={"name": "Weight", "template": "weight"}
    )
    base = f"/api/v1/trackers/{created.json()['id']}"
    entry = await api.client.post(f"{base}/entries", headers=anna, json={"value": 70.5})
    entry_url = f"{base}/entries/{entry.json()['id']}"
    for method, url, body in (
        ("GET", base, None),
        ("PATCH", base, {"name": "Mine"}),
        ("DELETE", base, None),
        ("GET", f"{base}/entries", None),
        ("POST", f"{base}/entries", {"value": 1}),
        ("PATCH", entry_url, {"value": 1}),
        ("DELETE", entry_url, None),
        ("GET", f"{base}/stats", None),
    ):
        response = await api.client.request(method, url, headers=boris, json=body)
        assert response.status_code == 404, (method, url)
    assert (await api.client.get("/api/v1/trackers", headers=boris)).json() == []
    unauthenticated = await api.client.get("/api/v1/trackers")
    assert unauthenticated.status_code == 401
