"""titan-admin: commands the admin runs in the app's container."""

import argparse
import asyncio
import os
import sys

from titan import db


async def add_user(name: str) -> int:
    # The CLI trims what the user types, so a name with spaces around it
    # could never sign in.
    name = name.strip()
    if not name:
        print("titan-admin: the name is empty", file=sys.stderr)
        return 1
    engine = db.make_engine(os.environ["TITAN_DATABASE_URL"])
    try:
        async with db.transaction(engine):
            code = await db.add_user(name)
    except db.NameTaken:
        print(f"titan-admin: the user {name!r} already exists", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"One-time sign-in code for {name}: {code}")
    print("They sign in with: titan login <node-url>")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="titan-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add-user", help="add a user and print a sign-in code")
    add.add_argument("name")
    args = parser.parse_args(argv)
    return asyncio.run(add_user(args.name))
