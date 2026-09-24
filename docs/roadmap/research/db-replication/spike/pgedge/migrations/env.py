"""Plain Alembic env: online mode only, one transaction per migration run.

Nothing Spock-specific is needed here: with spock.enable_ddl_replication=on
(set in postgresql.conf) every DDL statement Alembic issues on the node it is
connected to is captured and replicated through the `ddl_sql` replication set,
and new tables with a primary key are added to the `default` set
(spock.include_ddl_repset=on), so the alembic_version row replicates as well.
"""
from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
url = context.get_x_argument(as_dictionary=True).get("url") or config.get_main_option("sqlalchemy.url")


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline mode not used in the spike")
run_migrations_online()
