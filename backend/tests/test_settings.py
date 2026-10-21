import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from titan import db

SignIn = Callable[[], dict[str, str]]

KEY = "sk-test-key-1234567890"
BIG = {"url": "https://models.example/v1", "key": KEY}
LOCAL = {"url": "http://192.168.1.10:11434/v1"}


def models(client: TestClient, user: dict[str, str]) -> list[dict[str, object]]:
    response = client.get("/settings", headers=user)
    assert response.status_code == 200
    result: list[dict[str, object]] = response.json()["models"]
    return result


def test_no_models_at_first(client: TestClient, sign_in: SignIn) -> None:
    assert models(client, sign_in()) == []


def test_settings_show_the_models_but_not_the_keys(
    client: TestClient, sign_in: SignIn
) -> None:
    user = sign_in()
    assert client.put("/settings/models/big", json=BIG, headers=user).status_code == 204
    response = client.get("/settings", headers=user)
    assert response.json() == {
        "models": [{"name": "big", "url": BIG["url"], "has_key": True, "current": True}]
    }
    assert KEY not in response.text


@pytest.mark.parametrize("key", [None, ""])
def test_model_without_a_key(
    client: TestClient, sign_in: SignIn, key: str | None
) -> None:
    user = sign_in()
    client.put("/settings/models/llama", json={**LOCAL, "key": key}, headers=user)
    assert models(client, user)[0]["has_key"] is False


def test_a_user_has_several_models_and_one_current(
    client: TestClient, sign_in: SignIn
) -> None:
    user = sign_in()
    client.put("/settings/models/big", json=BIG, headers=user)
    client.put("/settings/models/llama", json=LOCAL, headers=user)
    # The first model stays current until the user picks another.
    assert [(m["name"], m["current"]) for m in models(client, user)] == [
        ("big", True),
        ("llama", False),
    ]
    use = client.put("/settings/current-model", json={"name": "llama"}, headers=user)
    assert use.status_code == 204
    assert [(m["name"], m["current"]) for m in models(client, user)] == [
        ("big", False),
        ("llama", True),
    ]


def test_saving_a_name_again_replaces_that_model(
    client: TestClient, sign_in: SignIn
) -> None:
    user = sign_in()
    client.put("/settings/models/big", json=BIG, headers=user)
    client.put("/settings/models/big", json=LOCAL, headers=user)
    assert models(client, user) == [
        {"name": "big", "url": LOCAL["url"], "has_key": False, "current": True}
    ]


@pytest.mark.parametrize("slash", ["/", "%2F"])
def test_a_name_may_hold_a_slash(
    client: TestClient, sign_in: SignIn, slash: str
) -> None:
    # The CLI sends the slash escaped, as %2F.
    user = sign_in()
    path = f"/settings/models/openai{slash}gpt-4o"
    assert client.put(path, json=BIG, headers=user).status_code == 204
    assert models(client, user)[0]["name"] == "openai/gpt-4o"
    assert client.delete(path, headers=user).status_code == 204
    assert models(client, user) == []


def test_names_are_trimmed_the_same_way_everywhere(
    client: TestClient, sign_in: SignIn
) -> None:
    user = sign_in()
    client.put("/settings/models/%20big%20", json=BIG, headers=user)
    assert models(client, user)[0]["name"] == "big"
    use = client.put("/settings/current-model", json={"name": " big "}, headers=user)
    assert use.status_code == 204
    assert client.delete("/settings/models/%20big", headers=user).status_code == 204
    assert models(client, user) == []


def test_two_first_models_at_once_leave_one_current(migrated: str, name: str) -> None:
    async def run() -> list[db.Model]:
        engine = db.make_engine(migrated)
        try:
            async with db.transaction(engine):
                code = await db.add_user(name)
                user_id = await db.user_of_token(await db.sign_in(name, code) or "")
            assert user_id is not None
            saved, release = asyncio.Event(), asyncio.Event()

            async def first() -> None:
                async with db.transaction(engine):
                    await db.save_model(user_id, db.Model("a", LOCAL["url"], None))
                    saved.set()
                    await release.wait()

            async def second() -> None:
                await saved.wait()
                async with db.transaction(engine):
                    await db.save_model(user_id, db.Model("b", LOCAL["url"], None))

            tasks = asyncio.gather(first(), second())
            await saved.wait()
            # Lets the second save reach the database while the first is open.
            await asyncio.sleep(0.5)
            release.set()
            await tasks
            async with db.transaction(engine):
                return await db.models_of(user_id)
        finally:
            await engine.dispose()

    assert [(m.name, m.current) for m in asyncio.run(run())] == [
        ("a", True),
        ("b", False),
    ]


def test_remove_a_model(client: TestClient, sign_in: SignIn) -> None:
    user = sign_in()
    client.put("/settings/models/big", json=BIG, headers=user)
    client.put("/settings/models/llama", json=LOCAL, headers=user)
    assert client.delete("/settings/models/big", headers=user).status_code == 204
    # The current model is gone, so no model is current until one is picked.
    assert models(client, user) == [
        {"name": "llama", "url": LOCAL["url"], "has_key": False, "current": False}
    ]


def test_unknown_model_is_404(client: TestClient, sign_in: SignIn) -> None:
    user = sign_in()
    client.put("/settings/models/big", json=BIG, headers=user)
    assert client.delete("/settings/models/nope", headers=user).status_code == 404
    use = client.put("/settings/current-model", json={"name": "nope"}, headers=user)
    assert use.status_code == 404
    # The refused pick left the current model as it was.
    assert models(client, user)[0]["current"] is True


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/settings/models/big", {"url": ""}),
        ("/settings/models/big", {"url": "  "}),
        ("/settings/models/%20", BIG),
    ],
)
def test_empty_url_or_name_is_refused(
    client: TestClient, sign_in: SignIn, path: str, body: dict[str, str]
) -> None:
    user = sign_in()
    assert client.put(path, json=body, headers=user).status_code == 422
    assert models(client, user) == []


@pytest.mark.parametrize(
    "url", ["ftp://models.example/v1", "models.example/v1", "https://", "file:///v1"]
)
def test_url_must_be_http_with_a_host(
    client: TestClient, sign_in: SignIn, url: str
) -> None:
    user = sign_in()
    response = client.put("/settings/models/big", json={"url": url}, headers=user)
    assert response.status_code == 422
    assert "http:// or https://" in response.text
    assert models(client, user) == []


def test_key_is_stored_encrypted_with_the_node_key(
    client: TestClient, sign_in: SignIn, migrated: str, key_file: str
) -> None:
    user = sign_in()
    client.put("/settings/models/big", json=BIG, headers=user)
    name = client.get("/me", headers=user).json()["name"]
    engine = create_engine(migrated)
    try:
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT m.encrypted_key FROM models m"
                    " JOIN users u ON u.id = m.user_id WHERE u.name = :name"
                ),
                {"name": name},
            ).scalar_one()
    finally:
        engine.dispose()
    assert KEY.encode() not in stored
    assert Fernet(Path(key_file).read_bytes()).decrypt(stored) == KEY.encode()


def test_a_user_sees_and_changes_only_their_own_models(
    client: TestClient, sign_in: SignIn
) -> None:
    alice, bob = sign_in(), sign_in()
    client.put("/settings/models/big", json=BIG, headers=alice)
    assert models(client, bob) == []
    # Bob's model of the same name is his own; Alice's stays as it was.
    client.put("/settings/models/big", json=LOCAL, headers=bob)
    assert client.delete("/settings/models/big", headers=bob).status_code == 204
    use = client.put("/settings/current-model", json={"name": "big"}, headers=bob)
    assert use.status_code == 404
    assert models(client, alice) == [
        {"name": "big", "url": BIG["url"], "has_key": True, "current": True}
    ]


def test_logs_never_hold_the_key(
    client: TestClient, sign_in: SignIn, caplog: pytest.LogCaptureFixture
) -> None:
    user = sign_in()
    # Every logger at its most verbose, the SQL log of SQLAlchemy too.
    caplog.set_level(logging.DEBUG)
    client.put("/settings/models/big", json=BIG, headers=user)
    client.get("/settings", headers=user)
    assert caplog.records
    assert KEY not in caplog.text
