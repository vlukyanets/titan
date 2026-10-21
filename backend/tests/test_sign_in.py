import asyncio
import logging
import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from titan import db
from titan.app import create_app

# The only routes a request without a token may reach.
PUBLIC = {"/health", "/sign-in"}


def add_user(url: str, name: str) -> str:
    async def run() -> str:
        engine = db.make_engine(url)
        try:
            async with db.transaction(engine):
                return await db.add_user(name)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture
def app(migrated: str) -> FastAPI:
    return create_app(migrated)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def test_code_signs_in_and_token_works(
    client: TestClient, migrated: str, name: str
) -> None:
    code = add_user(migrated, name)
    response = client.post("/sign-in", json={"name": name, "code": code})
    assert response.status_code == 200
    token = response.json()["token"]
    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json() == {"name": name}


def test_code_works_once(client: TestClient, migrated: str, name: str) -> None:
    code = add_user(migrated, name)
    assert client.post("/sign-in", json={"name": name, "code": code}).status_code == 200
    assert client.post("/sign-in", json={"name": name, "code": code}).status_code == 401


def test_wrong_code_or_name_is_refused(
    client: TestClient, migrated: str, name: str
) -> None:
    code = add_user(migrated, name)
    assert client.post("/sign-in", json={"name": name, "code": "x"}).status_code == 401
    assert (
        client.post("/sign-in", json={"name": "nobody", "code": code}).status_code
        == 401
    )
    # The refused tries did not spend the code.
    assert client.post("/sign-in", json={"name": name, "code": code}).status_code == 200


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic abc", "Bearer"])
def test_every_route_needs_a_token(
    app: FastAPI, client: TestClient, authorization: str | None
) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert len(routes) == len(app.routes), "a route that is not an APIRoute"
    private = [r for r in routes if r.path not in PUBLIC]
    assert private
    for route in private:
        path = re.sub(r"\{[^}]*\}", "1", route.path)
        for method in route.methods or ():
            response = client.request(method, path, headers=headers)
            assert response.status_code == 401, f"{method} {route.path}"


def test_node_stores_only_hashes(client: TestClient, migrated: str, name: str) -> None:
    code = add_user(migrated, name)
    token = client.post("/sign-in", json={"name": name, "code": code}).json()["token"]
    engine = create_engine(migrated)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT u.*, t.* FROM users u JOIN tokens t ON t.user_id = u.id")
            ).all()
    finally:
        engine.dispose()
    stored = " ".join(str(value) for row in rows for value in row)
    assert code not in stored
    assert token not in stored


def test_logs_never_hold_a_token_or_code(
    client: TestClient, migrated: str, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Every logger at its most verbose, the SQL log of SQLAlchemy too.
    caplog.set_level(logging.DEBUG)
    code = add_user(migrated, name)
    token = client.post("/sign-in", json={"name": name, "code": code}).json()["token"]
    client.post("/sign-in", json={"name": name, "code": code})
    client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert caplog.records
    assert code not in caplog.text
    assert token not in caplog.text
