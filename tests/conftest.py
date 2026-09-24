"""Shared fixtures. Database tests run only when TITAN_TEST_DATABASE_URL is set."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from titan.api.app import create_app
from titan.settings import Settings

# Nothing listens here; tests that do not touch the database use it as a placeholder.
UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"


def test_database_url() -> str:
    url = os.environ.get("TITAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("TITAN_TEST_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
def migrated_db_url() -> str:
    """The test database, upgraded to the latest revision once per session."""
    from alembic import command

    from titan.cli.main import alembic_config

    url = test_database_url()
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture
def db_url(migrated_db_url: str) -> str:
    """The migrated test database with every table emptied before the test."""
    import sqlalchemy as sa

    engine = sa.create_engine(migrated_db_url)
    with engine.begin() as conn:
        conn.execute(sa.text("TRUNCATE notifications, push_subscriptions, devices, users"))
    engine.dispose()
    return migrated_db_url


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(database_url=UNREACHABLE_DB))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.engine.dispose()


@pytest.fixture
async def db_client(db_url: str) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(database_url=db_url))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.engine.dispose()
