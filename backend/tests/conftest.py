import asyncio
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from fake_model import KEY, URL, FakeModel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from titan import db
from titan.app import create_app

BACKEND = Path(__file__).parent.parent


@pytest.fixture(scope="session")
def database_url() -> str:
    """The test database that make check starts from tests/compose.test.yaml."""
    url = os.environ.get("TITAN_TEST_DATABASE_URL")
    if not url:
        pytest.fail("TITAN_TEST_DATABASE_URL is not set; run the tests with make check")
    return url


@pytest.fixture(scope="session")
def alembic_config(database_url: str) -> Config:
    os.environ["TITAN_DATABASE_URL"] = database_url
    return Config(toml_file=str(BACKEND / "pyproject.toml"))


@pytest.fixture(scope="session")
def migrated(alembic_config: Config, database_url: str) -> str:
    """The test database with every migration applied."""
    command.upgrade(alembic_config, "head")
    return database_url


@pytest.fixture
def key_file(tmp_path: Path) -> str:
    """Where the app keeps its node key; the file does not exist yet."""
    return str(tmp_path / "node.key")


def unique_name() -> str:
    # The tests share one database, so every user gets a name of its own.
    return f"user-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def name() -> str:
    return unique_name()


@pytest.fixture
def add_user(migrated: str) -> Callable[[str], str]:
    """Adds a user as titan-admin does and returns their sign-in code."""

    def add(name: str) -> str:
        async def run() -> str:
            engine = db.make_engine(migrated)
            try:
                async with db.transaction(engine):
                    return await db.add_user(name)
            finally:
                await engine.dispose()

        return asyncio.run(run())

    return add


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def app(migrated: str, key_file: str, model: FakeModel) -> FastAPI:
    return create_app(migrated, key_file, httpx2.MockTransport(model))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def sign_in(
    client: TestClient, add_user: Callable[[str], str]
) -> Callable[[], dict[str, str]]:
    """Signs in a new user and returns the headers of their requests."""

    def sign_in() -> dict[str, str]:
        name = unique_name()
        code = add_user(name)
        token = client.post("/sign-in", json={"name": name, "code": code}).json()[
            "token"
        ]
        return {"Authorization": f"Bearer {token}"}

    return sign_in


@pytest.fixture
def user(client: TestClient, sign_in: Callable[[], dict[str, str]]) -> dict[str, str]:
    """A signed-in user whose current model is "big" at URL."""
    headers = sign_in()
    put = client.put(
        "/settings/models/big", json={"url": URL, "key": KEY}, headers=headers
    )
    assert put.status_code == 204
    return headers
