"""The `titan` command: node administration, and the client commands.

Administration runs on a node against its database. The client commands in
`titan.cli.commands` talk to a node's HTTP API from any machine (docs/spec/cli.md).

`main()` takes its settings, standard input, client login file and HTTP transport
as arguments, so callers other than the shell (tests, scripts) pass their own
instead of changing the process environment. Without them it reads TITAN_*
variables, the real stdin and the saved login.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

import titan.migrations
from titan.cli import commands
from titan.cli.client import config_path
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


def openapi_schema() -> dict[str, object]:
    from titan.api.app import create_app

    # The schema does not depend on the database; a placeholder URL avoids needing one.
    settings = Settings(database_url="postgresql+psycopg://openapi@localhost/openapi")
    return create_app(settings).openapi()


def write_openapi(path: Path) -> None:
    path.write_text(json.dumps(openapi_schema(), indent=2, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class Cli:
    """What the commands depend on besides their arguments."""

    # Read on first use, so commands that need no database run without TITAN_* set.
    load_settings: Callable[[], Settings]
    stdin: TextIO

    def settings(self) -> Settings:
        return self.load_settings()

    # ------------------------------------------------------------- migrate

    def migrate(self, target: str) -> None:
        """Upgrade the schema of the configured database.

        The advisory lock only serialises runs against the same node. Under Spock the
        DDL replicates to the other nodes, so migrations are started on one node at a
        time (ADR 0006, docs/architecture/database-migrations.md).
        """
        url = self.settings().database_url.get_secret_value()
        sync_url = url.replace("+asyncpg", "+psycopg")
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            conn.execute(text("select pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
            try:
                command.upgrade(alembic_config(url), target)
            finally:
                conn.execute(text("select pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
        engine.dispose()

    # --------------------------------------------------------------- users

    def read_new_password(self, from_stdin: bool) -> str:
        if from_stdin:
            return self.stdin.readline().rstrip("\n")
        first = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != first:
            raise SystemExit("passwords do not match")
        return first

    async def create_user(
        self, username: str, password: str, owner: bool, display_name: str | None
    ) -> str:
        from titan.domains.accounts.models import Role
        from titan.domains.accounts.service import AccountsService
        from titan.storage.db import create_engine as create_async_engine
        from titan.storage.db import session_factory

        engine = create_async_engine(self.settings())
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

    async def list_users(self) -> list[str]:
        from titan.domains.accounts.service import AccountsService
        from titan.storage.db import create_engine as create_async_engine
        from titan.storage.db import session_factory

        engine = create_async_engine(self.settings())
        try:
            async with session_factory(engine)() as session:
                users = await AccountsService(session).list_users(actor=None)
                return [f"{u.username}\t{u.role.value}\t{u.display_name}" for u in users]
        finally:
            await engine.dispose()

    def users_command(self, args: argparse.Namespace) -> int:
        from titan.domains.accounts.errors import AccountsError

        try:
            if args.users_command == "create":
                password = self.read_new_password(args.password_stdin)
                print(
                    asyncio.run(
                        self.create_user(args.username, password, args.owner, args.display_name)
                    )
                )
            else:
                for line in asyncio.run(self.list_users()):
                    print(line)
        except AccountsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    # ------------------------------------------------------- notifications

    async def send_notification(self, username: str, title: str, body: str) -> str:
        from titan.domains.accounts.service import AccountsService
        from titan.domains.notifications.models import NotificationKind
        from titan.domains.notifications.service import NotificationsService
        from titan.notify import UnifiedPushSender, new_client
        from titan.storage.db import create_engine as create_async_engine
        from titan.storage.db import session_factory

        settings = self.settings()
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

    def notifications_command(self, args: argparse.Namespace) -> int:
        from titan.domains.accounts.errors import AccountsError

        try:
            print(asyncio.run(self.send_notification(args.username, args.title, args.body)))
        except AccountsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    # --------------------------------------------------------------- usage

    async def usage(self, month: str) -> list[str]:
        from titan.domains.usage.service import UsageService
        from titan.storage.db import create_engine as create_async_engine
        from titan.storage.db import session_factory

        engine = create_async_engine(self.settings())
        try:
            async with session_factory(engine)() as session:
                members = await UsageService(session).household(None, month)
        finally:
            await engine.dispose()
        lines = [f"{month}\tsessions\tinput\toutput\tcache read\tcache write\tcost USD"]
        for member in members:
            t = member.usage.total
            lines.append(
                f"{member.username}\t{t.sessions}\t{t.input_tokens}\t{t.output_tokens}\t"
                f"{t.cache_read_tokens}\t{t.cache_creation_tokens}\t{t.cost_usd:.4f}"
            )
        return lines

    def usage_command(self, args: argparse.Namespace) -> int:
        from titan.domains.usage.errors import UsageError
        from titan.domains.usage.service import current_month

        try:
            for line in asyncio.run(self.usage(args.month or current_month())):
                print(line)
        except UsageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    # -------------------------------------------------------------- budget

    async def budgets(self) -> list[str]:
        from titan.domains.usage.budget import BudgetService
        from titan.domains.usage.service import current_month
        from titan.storage.db import create_engine as create_async_engine
        from titan.storage.db import session_factory

        engine = create_async_engine(self.settings())
        try:
            async with session_factory(engine)() as session:
                members = await BudgetService(session).household(None)
        finally:
            await engine.dispose()
        month = members[0].status.month if members else current_month()
        lines = [f"{month}\tlimit USD\tspent USD\tstate\towner alerts"]
        for member in members:
            status = member.status
            limit = "none" if status.limit_usd is None else str(status.limit_usd)
            alerts = status.owner_alerts.value if status.owner_alerts else "-"
            lines.append(
                f"{member.username}\t{limit}\t{status.spent_usd:.4f}\t{status.state.value}"
                f"\t{alerts}"
            )
        return lines

    async def set_budget(
        self, username: str, limit: Decimal | None, owner_alerts: str | None = None
    ) -> str:
        from titan.domains.accounts.service import AccountsService
        from titan.domains.notifications.service import NotificationsService
        from titan.domains.usage.budget import BudgetService
        from titan.domains.usage.models import OwnerAlerts
        from titan.notify import UnifiedPushSender, new_client
        from titan.storage.db import create_engine as create_async_engine
        from titan.storage.db import session_factory

        settings = self.settings()
        engine = create_async_engine(settings)
        client = new_client(settings.push_timeout_seconds)
        try:
            async with session_factory(engine)() as session:
                user = await AccountsService(session).get_user_by_username(username)
                notifications = NotificationsService(session, pusher=UnifiedPushSender(client))
                member = await BudgetService(session, notifications=notifications).set_limit(
                    None,
                    user.id,
                    limit,
                    owner_alerts=OwnerAlerts(owner_alerts) if owner_alerts else None,
                )
        finally:
            await client.aclose()
            await engine.dispose()
        status = member.status
        if status.limit_usd is None:
            return f"{member.username}: no limit"
        alerts = status.owner_alerts.value if status.owner_alerts else "-"
        return (
            f"{member.username}: {status.limit_usd} USD a month, "
            f"{status.spent_usd:.2f} spent in {status.month}, {status.state.value}, "
            f"owner alerts {alerts}"
        )

    def budget_command(self, args: argparse.Namespace) -> int:
        from titan.domains.accounts.errors import AccountsError
        from titan.domains.usage.errors import UsageError

        try:
            if args.budget_command == "set":
                limit = None if args.limit.lower() == "none" else _decimal(args.limit)
                print(asyncio.run(self.set_budget(args.username, limit, args.owner_alerts)))
            else:
                for line in asyncio.run(self.budgets()):
                    print(line)
        except (AccountsError, UsageError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    # -------------------------------------------------------------- claude

    def claude_check(self) -> int:
        """Verify that Claude Code uses exactly the credential of the configured mode.

        Cleans this process's environment, as the agent runtime does at startup.
        """
        from claude_agent_sdk import ClaudeSDKError

        from titan.agent import auth
        from titan.agent.node import self_check

        settings = self.settings()
        try:
            mode = auth.install(settings)
            source = asyncio.run(self_check(settings))
        except auth.ClaudeAuthError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ClaudeSDKError as exc:
            print(f"error: Claude Code could not start: {type(exc).__name__}", file=sys.stderr)
            return 1
        print(f"ok: {mode.value} mode, credential source {source}")
        return 0


def _decimal(value: str) -> Decimal:
    from titan.domains.usage.errors import InvalidBudgetError

    try:
        return Decimal(value)
    except InvalidOperation:
        raise InvalidBudgetError(f"not an amount: {value!r}") from None


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="titan", description="TITAN: node administration and the client commands"
    )
    sub = root.add_subparsers(dest="command", required=True)
    commands.add_parsers(sub)
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
    c = sub.add_parser("claude", help="Claude credentials")
    csub = c.add_subparsers(dest="claude_command", required=True)
    csub.add_parser("check", help="check that only the configured credential is used")
    n = sub.add_parser("notifications", help="send notifications")
    nsub = n.add_subparsers(dest="notifications_command", required=True)
    ns = nsub.add_parser("send", help="send a system notification to a user's devices")
    ns.add_argument("username")
    ns.add_argument("title")
    ns.add_argument("--body", default="")
    us = sub.add_parser("usage", help="token usage and cost of every user for a month")
    us.add_argument("--month", help="YYYY-MM in UTC, the current month by default")
    b = sub.add_parser("budget", help="monthly budgets")
    bsub = b.add_subparsers(dest="budget_command", required=True)
    bsub.add_parser("list", help="every user's limit, spending and state this month")
    bs = bsub.add_parser("set", help="set a user's monthly limit in USD")
    bs.add_argument("username")
    bs.add_argument("limit", help="amount in USD, or none to remove the cap")
    bs.add_argument(
        "--owner-alerts",
        choices=("off", "exceeded", "all"),
        help="which states the owners hear about; unchanged, or exceeded for a new limit",
    )
    return root


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    stdin: TextIO | None = None,
    client_config: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    args = parser().parse_args(argv)
    if args.command in commands.CLIENT_COMMANDS:
        client = commands.ClientCommands(
            config=client_config or config_path(os.environ),
            stdin=stdin if stdin is not None else sys.stdin,
            out=sys.stdout,
            err=sys.stderr,
            transport=transport,
        )
        return asyncio.run(commands.run(client, args))
    cli = Cli(
        load_settings=(lambda: settings) if settings is not None else Settings,
        stdin=stdin if stdin is not None else sys.stdin,
    )
    if args.command == "migrate":
        cli.migrate(args.target)
    elif args.command == "users":
        return cli.users_command(args)
    elif args.command == "notifications":
        return cli.notifications_command(args)
    elif args.command == "usage":
        return cli.usage_command(args)
    elif args.command == "budget":
        return cli.budget_command(args)
    elif args.command == "claude":
        return cli.claude_check()
    elif args.command == "openapi":
        write_openapi(args.output)
        print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
