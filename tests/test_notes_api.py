"""Notes and memories over the API."""

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


async def test_a_note_is_written_shared_found_and_deleted(api: Api) -> None:
    anna_user = await api.create_user("anna", OWNER_PW, Role.OWNER)
    boris_user = await api.create_user("boris", MEMBER_PW, Role.MEMBER)
    anna = auth(await api.token("anna", OWNER_PW))
    boris = auth(await api.token("boris", MEMBER_PW))

    created = await api.client.post(
        "/api/v1/notes",
        headers=anna,
        json={
            "title": "Dacha",
            "body": "# Wi-Fi\n\nfake-password-123, роутер в шкафу",
            "tags": ["Home"],
            "shared_with": [str(boris_user.id)],
        },
    )
    assert created.status_code == 201, created.text
    note = created.json()
    assert (note["owner_id"], note["tags"]) == (str(anna_user.id), ["home"])
    assert note["shared_with"] == [str(boris_user.id)]
    url = f"/api/v1/notes/{note['id']}"

    found = await api.client.get("/api/v1/notes", headers=boris, params={"q": "ШКАФ wi-fi"})
    assert found.status_code == 200, found.text
    assert found.json() == [
        {
            "id": note["id"],
            "owner_id": str(anna_user.id),
            "title": "Dacha",
            "excerpt": "# Wi-Fi fake-password-123, роутер в шкафу",
            "tags": ["home"],
            "shared_with": [str(boris_user.id)],
            "created_at": note["created_at"],
            "updated_at": note["updated_at"],
        }
    ]
    mine = await api.client.get("/api/v1/notes", headers=boris, params={"shared": "mine"})
    assert mine.json() == []
    with_me = await api.client.get("/api/v1/notes", headers=boris, params={"shared": "with_me"})
    assert [n["id"] for n in with_me.json()] == [note["id"]]
    read = await api.client.get(url, headers=boris)
    assert read.json()["body"].startswith("# Wi-Fi")

    refused = await api.client.patch(url, headers=boris, json={"title": "Mine"})
    assert refused.status_code == 403
    assert refused.headers["content-type"] == "application/problem+json"
    assert (await api.client.delete(url, headers=boris)).status_code == 403

    patched = await api.client.patch(url, headers=anna, json={"shared_with": [], "tags": None})
    assert patched.status_code == 200, patched.text
    assert (patched.json()["shared_with"], patched.json()["tags"]) == ([], [])
    assert (await api.client.get(url, headers=boris)).status_code == 404

    assert (await api.client.delete(url, headers=anna)).status_code == 204
    assert (await api.client.get(url, headers=anna)).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"title": " ", "body": ""},
        {"title": "x" * 201},
        {"title": "Tags", "tags": ["t" * 33]},
        {"title": "Nobody", "shared_with": ["00000000-0000-7000-8000-000000000000"]},
        {"title": "Extra", "owner_id": "00000000-0000-7000-8000-000000000000"},
    ],
)
async def test_invalid_notes_are_refused(api: Api, body: dict[str, object]) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    anna = auth(await api.token("anna", OWNER_PW))
    response = await api.client.post("/api/v1/notes", headers=anna, json=body)
    assert response.status_code == 422, response.text


async def test_memories_are_listed_corrected_and_forgotten(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", MEMBER_PW, Role.MEMBER)
    anna = auth(await api.token("anna", OWNER_PW))
    boris = auth(await api.token("boris", MEMBER_PW))

    first = await api.client.post(
        "/api/v1/memories", headers=anna, json={"statement": "Prefers  morning meetings"}
    )
    assert first.status_code == 201, first.text
    memory = first.json()
    assert (memory["statement"], memory["source"], memory["source_id"]) == (
        "Prefers morning meetings",
        "user",
        None,
    )
    assert memory["confidence"] == 1.0
    again = await api.client.post(
        "/api/v1/memories", headers=anna, json={"statement": "prefers MORNING meetings"}
    )
    assert again.json()["id"] == memory["id"]
    second = await api.client.post(
        "/api/v1/memories", headers=anna, json={"statement": "Живёт в Днепре"}
    )
    url = f"/api/v1/memories/{memory['id']}"

    listed = await api.client.get("/api/v1/memories", headers=anna)
    assert [m["id"] for m in listed.json()] == [second.json()["id"], memory["id"]]
    searched = await api.client.get("/api/v1/memories", headers=anna, params={"q": "днепр"})
    assert [m["id"] for m in searched.json()] == [second.json()["id"]]

    corrected = await api.client.patch(
        url, headers=anna, json={"statement": "Prefers meetings after lunch"}
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["statement"] == "Prefers meetings after lunch"
    empty = await api.client.patch(url, headers=anna, json={"statement": ""})
    assert empty.status_code == 422

    assert (await api.client.get("/api/v1/memories", headers=boris)).json() == []
    assert (await api.client.patch(url, headers=boris, json={"statement": "x"})).status_code == 404
    assert (await api.client.delete(url, headers=boris)).status_code == 404
    assert (await api.client.delete(url, headers=anna)).status_code == 204
    assert (await api.client.delete(url, headers=anna)).status_code == 404
    assert (await api.client.get("/api/v1/memories")).status_code == 401
