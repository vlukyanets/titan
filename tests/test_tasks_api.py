"""Projects and tasks over the API."""

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


async def test_a_task_goes_from_creation_to_completion(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    created = await api.client.post(
        "/api/v1/tasks",
        headers=anna,
        json={
            "title": "Water the plants",
            "due_at": "2026-09-21T09:00:00+03:00",
            "recurrence": "FREQ=WEEKLY;BYDAY=MO",
            "estimate_minutes": 10,
            "tags": ["Home"],
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["due_at"] == "2026-09-21T06:00:00Z"
    assert (task["status"], task["priority"], task["tags"]) == ("todo", 4, ["home"])

    patched = await api.client.patch(
        f"/api/v1/tasks/{task['id']}",
        headers=anna,
        json={"priority": 1, "notes": "The balcony ones too", "estimate_minutes": None},
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["priority"], patched.json()["estimate_minutes"]) == (1, None)
    assert patched.json()["recurrence"] == "FREQ=WEEKLY;BYDAY=MO"

    done = await api.client.post(f"/api/v1/tasks/{task['id']}/complete", headers=anna)
    assert done.status_code == 200, done.text
    assert done.json()["task"]["status"] == "done"
    following = done.json()["next"]
    assert following["status"] == "todo"
    assert following["due_at"] > task["due_at"]
    assert following["due_at"].endswith("T06:00:00Z")
    again = await api.client.post(f"/api/v1/tasks/{task['id']}/complete", headers=anna)
    assert again.status_code == 409

    todo = await api.client.get("/api/v1/tasks", headers=anna, params={"status": "todo"})
    assert [t["id"] for t in todo.json()] == [following["id"]]
    both = await api.client.get(
        "/api/v1/tasks", headers=anna, params=[("status", "todo"), ("status", "done")]
    )
    assert len(both.json()) == 2

    deleted = await api.client.delete(f"/api/v1/tasks/{task['id']}", headers=anna)
    assert deleted.status_code == 204
    gone = await api.client.get(f"/api/v1/tasks/{task['id']}", headers=anna)
    assert gone.status_code == 404
    assert gone.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(
    "body",
    [
        {"title": ""},
        {"title": "x", "priority": 0},
        {"title": "x", "due_at": "2026-10-01T09:00:00"},
        {"title": "x", "recurrence": "FREQ=MINUTELY"},
        {"title": "x", "recurrence": "FREQ=DAILY"},
        {"title": "x", "owner_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
async def test_invalid_tasks_are_refused(api: Api, body: dict[str, object]) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    response = await api.client.post("/api/v1/tasks", headers=anna, json=body)
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/problem+json"


async def test_a_patch_cannot_null_a_required_field(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    task = (await api.client.post("/api/v1/tasks", headers=anna, json={"title": "Read"})).json()
    for body in ({"title": None}, {"priority": None}, {"status": None}, {"status": "done"}):
        response = await api.client.patch(f"/api/v1/tasks/{task['id']}", headers=anna, json=body)
        assert response.status_code == 422, (body, response.text)
    still = await api.client.get(f"/api/v1/tasks/{task['id']}", headers=anna)
    assert (still.json()["title"], still.json()["priority"]) == ("Read", 4)


async def test_a_shared_project_is_visible_only_to_its_members(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    boris_user = await api.create_user("boris", MEMBER_PW)
    await api.create_user("carla", "carla-password-123")
    anna = auth(await api.token("anna", OWNER_PW))
    boris = auth(await api.token("boris", MEMBER_PW))
    carla = auth(await api.token("carla", "carla-password-123"))

    created = await api.client.post(
        "/api/v1/projects",
        headers=anna,
        json={"title": "Home", "shared_with": [str(boris_user.id)]},
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["shared_with"] == [str(boris_user.id)]
    assert project["status"] == "active"

    task = await api.client.post(
        "/api/v1/tasks", headers=boris, json={"title": "Fix the tap", "project_id": project["id"]}
    )
    assert task.status_code == 201, task.text
    listed = await api.client.get(
        "/api/v1/tasks", headers=anna, params={"project_id": project["id"]}
    )
    assert [t["title"] for t in listed.json()] == ["Fix the tap"]

    assert (
        await api.client.get(f"/api/v1/projects/{project['id']}", headers=carla)
    ).status_code == 404
    assert (await api.client.get("/api/v1/projects", headers=carla)).json() == []
    assert (await api.client.get("/api/v1/tasks", headers=carla)).json() == []
    peek = await api.client.post(
        "/api/v1/tasks", headers=carla, json={"title": "Peek", "project_id": project["id"]}
    )
    assert peek.status_code == 404

    rename = await api.client.patch(
        f"/api/v1/projects/{project['id']}", headers=boris, json={"title": "Mine"}
    )
    assert rename.status_code == 403
    archived = await api.client.patch(
        f"/api/v1/projects/{project['id']}", headers=anna, json={"status": "archived"}
    )
    assert archived.json()["status"] == "archived"
    active = await api.client.get("/api/v1/projects", headers=boris, params={"status": "active"})
    assert active.json() == []

    deleted = await api.client.delete(f"/api/v1/projects/{project['id']}", headers=anna)
    assert deleted.status_code == 204
    moved = await api.client.get(f"/api/v1/tasks/{task.json()['id']}", headers=boris)
    assert moved.json()["project_id"] is None
