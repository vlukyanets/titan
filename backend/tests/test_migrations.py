import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def tables(url: str) -> list[str]:
    engine = create_engine(url)
    try:
        return sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_twice_changes_nothing(
    alembic_config: Config, database_url: str
) -> None:
    command.upgrade(alembic_config, "head")
    before = tables(database_url)
    command.upgrade(alembic_config, "head")
    assert tables(database_url) == before


@pytest.mark.usefixtures("migrated")
def test_migrations_match_the_models(alembic_config: Config) -> None:
    command.check(alembic_config)
