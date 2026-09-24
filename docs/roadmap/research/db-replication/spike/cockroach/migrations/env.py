"""Alembic environment for the CockroachDB spike (sqlalchemy-cockroachdb dialect)."""
import os

from alembic import context
from sqlalchemy import create_engine

from models import metadata  # head schema, used only by `alembic check`

config = context.config
if config.config_file_name is not None:
    from logging.config import fileConfig

    fileConfig(config.config_file_name)

url = context.get_x_argument(as_dictionary=True).get("url") or os.environ["TITAN_DB_URL"]


def run_migrations_online() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
