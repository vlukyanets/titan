import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

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
def name() -> str:
    """A user name no other test uses; the tests share one database."""
    return f"user-{uuid.uuid4().hex[:8]}"
