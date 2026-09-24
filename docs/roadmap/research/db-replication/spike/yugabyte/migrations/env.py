"""Alembic environment for the YugabyteDB spike (PostgreSQL dialect via psycopg 3)."""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or os.environ["SPIKE_DB_URL"]


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # Default Alembic behaviour for PostgreSQL: the whole upgrade runs in one
        # transaction (transactional_ddl=True). Kept as-is on purpose so the
        # spike records how YSQL treats DDL inside a transaction block.
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
