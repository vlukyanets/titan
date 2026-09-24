"""The `titan` command: administration from any node."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

import titan.migrations
from titan.settings import Settings

MIGRATIONS_DIR = Path(titan.migrations.__file__).resolve().parent
# Arbitrary constant, so concurrent `titan migrate` runs against one database serialise.
MIGRATION_LOCK_ID = 0x7174616E  # "qtan"


def alembic_config(url: str | None = None) -> Config:
    # Built in code rather than read from alembic.ini, which is not part of the installed
    # package. alembic.ini is only for developers running `uv run alembic ...`.
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(target: str) -> None:
    """Upgrade the schema of the database behind TITAN_DATABASE_URL.

    The advisory lock only serialises runs against the same node. Under Spock the DDL
    replicates to the other nodes, so migrations are started on one node at a time
    (ADR 0006, docs/architecture/database-migrations.md).
    """
    url = Settings().database_url.get_secret_value()
    sync_url = url.replace("+asyncpg", "+psycopg")
    engine = create_engine(sync_url)
    with engine.connect() as conn:
        conn.execute(text("select pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        try:
            command.upgrade(alembic_config(url), target)
        finally:
            conn.execute(text("select pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
    engine.dispose()


def openapi_schema() -> dict[str, object]:
    from titan.api.app import create_app

    # The schema does not depend on the database; a placeholder URL avoids needing one.
    settings = Settings(database_url="postgresql+psycopg://openapi@localhost/openapi")
    return create_app(settings).openapi()


def write_openapi(path: Path) -> None:
    path.write_text(json.dumps(openapi_schema(), indent=2, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="titan", description="TITAN administration")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("migrate", help="apply database migrations under a cluster-wide lock")
    m.add_argument("target", nargs="?", default="head")
    o = sub.add_parser("openapi", help="export the OpenAPI schema")
    o.add_argument("--output", type=Path, default=Path("docs/api/openapi.json"))
    args = parser.parse_args(argv)

    if args.command == "migrate":
        migrate(args.target)
    elif args.command == "openapi":
        write_openapi(args.output)
        print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
