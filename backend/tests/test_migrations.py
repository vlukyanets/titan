from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND = Path(__file__).parent.parent


def tables(url: str) -> list[str]:
    engine = create_engine(url)
    try:
        return sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_twice_changes_nothing(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TITAN_DATABASE_URL", database_url)
    config = Config(toml_file=str(BACKEND / "pyproject.toml"))
    command.upgrade(config, "head")
    before = tables(database_url)
    command.upgrade(config, "head")
    assert tables(database_url) == before
