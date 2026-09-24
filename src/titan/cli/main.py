"""The `titan` command: administration from any node."""

from __future__ import annotations

import argparse
import asyncio
import getpass
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


def _read_new_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise SystemExit("passwords do not match")
    return first


async def _create_user(username: str, password: str, owner: bool, display_name: str | None) -> str:
    from titan.domains.accounts.models import Role
    from titan.domains.accounts.service import AccountsService
    from titan.storage.db import create_engine as create_async_engine
    from titan.storage.db import session_factory

    engine = create_async_engine(Settings())
    try:
        async with session_factory(engine)() as session:
            user = await AccountsService(session).create_user(
                username,
                password,
                role=Role.OWNER if owner else Role.MEMBER,
                display_name=display_name,
            )
            return f"created {user.role.value} {user.username} ({user.id})"
    finally:
        await engine.dispose()


async def _list_users() -> list[str]:
    from titan.domains.accounts.service import AccountsService
    from titan.storage.db import create_engine as create_async_engine
    from titan.storage.db import session_factory

    engine = create_async_engine(Settings())
    try:
        async with session_factory(engine)() as session:
            users = await AccountsService(session).list_users(actor=None)
            return [f"{u.username}\t{u.role.value}\t{u.display_name}" for u in users]
    finally:
        await engine.dispose()


async def _send_notification(username: str, title: str, body: str) -> str:
    from titan.domains.accounts.service import AccountsService
    from titan.domains.notifications.models import NotificationKind
    from titan.domains.notifications.service import NotificationsService
    from titan.notify import UnifiedPushSender, new_client
    from titan.storage.db import create_engine as create_async_engine
    from titan.storage.db import session_factory

    settings = Settings()
    engine = create_async_engine(settings)
    client = new_client(settings.push_timeout_seconds)
    try:
        async with session_factory(engine)() as session:
            user = await AccountsService(session).get_user_by_username(username)
            notification = await NotificationsService(
                session, pusher=UnifiedPushSender(client)
            ).notify(user.id, NotificationKind.SYSTEM, title, body)
            state = "pushed" if notification.delivered_at else "stored, no push got through"
            return f"sent {notification.id} to {user.username}: {state}"
    finally:
        await client.aclose()
        await engine.dispose()


def notifications_command(args: argparse.Namespace) -> int:
    from titan.domains.accounts.errors import AccountsError

    try:
        print(asyncio.run(_send_notification(args.username, args.title, args.body)))
    except AccountsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def users_command(args: argparse.Namespace) -> int:
    from titan.domains.accounts.errors import AccountsError

    try:
        if args.users_command == "create":
            password = _read_new_password(args.password_stdin)
            print(asyncio.run(_create_user(args.username, password, args.owner, args.display_name)))
        else:
            for line in asyncio.run(_list_users()):
                print(line)
    except AccountsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="titan", description="TITAN administration")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("migrate", help="apply database migrations under a cluster-wide lock")
    m.add_argument("target", nargs="?", default="head")
    o = sub.add_parser("openapi", help="export the OpenAPI schema")
    o.add_argument("--output", type=Path, default=Path("docs/api/openapi.json"))
    u = sub.add_parser("users", help="manage accounts")
    usub = u.add_subparsers(dest="users_command", required=True)
    uc = usub.add_parser("create", help="create an account (the first one with --owner)")
    uc.add_argument("username")
    uc.add_argument("--owner", action="store_true", help="create the household owner")
    uc.add_argument("--display-name")
    uc.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    usub.add_parser("list", help="list accounts")
    n = sub.add_parser("notifications", help="send notifications")
    nsub = n.add_subparsers(dest="notifications_command", required=True)
    ns = nsub.add_parser("send", help="send a system notification to a user's devices")
    ns.add_argument("username")
    ns.add_argument("title")
    ns.add_argument("--body", default="")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        migrate(args.target)
    elif args.command == "users":
        return users_command(args)
    elif args.command == "notifications":
        return notifications_command(args)
    elif args.command == "openapi":
        write_openapi(args.output)
        print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
