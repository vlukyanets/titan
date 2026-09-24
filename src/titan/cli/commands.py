"""Client subcommands of `titan`: login, chat and approvals (docs/spec/cli.md).

Replies go to `out`; progress, hints and errors go to `err`, so a reply can be
piped. Every command returns the process exit code.
"""

from __future__ import annotations

import argparse
import getpass
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import httpx

from titan.cli.client import Api, ClientError, Login, normalize_server

DEVICE_NAME_LENGTH = 64


@dataclass
class ClientCommands:
    config: Path
    stdin: TextIO
    out: TextIO
    err: TextIO
    transport: httpx.AsyncBaseTransport | None = None
    ask_password: Callable[[str], str] = getpass.getpass

    def api(self, login: Login | None = None, server: str | None = None) -> Api:
        if login is not None:
            return Api(login.server, login.token, transport=self.transport)
        assert server is not None
        return Api(server, transport=self.transport)

    def say(self, message: str) -> None:
        print(message, file=self.err)

    def login_or_fail(self) -> Login:
        return Login.load(self.config)

    # ----------------------------------------------------------- login

    async def login(self, args: argparse.Namespace) -> int:
        server = normalize_server(args.server)
        if args.password_stdin:
            password = self.stdin.readline().rstrip("\n")
        else:
            password = self.ask_password(f"Password for {args.username}: ")
        name = (args.device_name or socket.gethostname() or "titan cli")[:DEVICE_NAME_LENGTH]
        async with self.api(server=server) as api:
            paired = await api.call(
                "POST",
                "/devices/pair",
                {
                    "username": args.username,
                    "password": password,
                    "device_name": name,
                    "platform": "cli",
                },
            )
        try:
            previous: Login | None = Login.load(self.config)
        except ClientError:
            previous = None
        login = Login(
            server=server,
            username=paired["user"]["username"],
            device_id=uuid.UUID(paired["device_id"]),
            token=paired["token"],
        )
        login.save(self.config)
        self.say(f"logged in to {server} as {login.username} (device {login.device_id})")
        if previous is not None:
            await self._revoke_previous(previous)
        return 0

    async def _revoke_previous(self, previous: Login) -> None:
        try:
            async with self.api(previous) as api:
                await api.call("DELETE", f"/devices/{previous.device_id}")
        except ClientError as exc:
            if exc.status != 401:  # a refused token is already useless
                self.say(
                    f"warning: could not revoke the previous device {previous.device_id} "
                    f"on {previous.server}; revoke it from another client"
                )
        else:
            self.say(f"revoked the previous device {previous.device_id}")

    async def logout(self, args: argparse.Namespace) -> int:
        login = self.login_or_fail()
        if not args.local:
            try:
                async with self.api(login) as api:
                    await api.call("DELETE", f"/devices/{login.device_id}")
            except ClientError as exc:
                if exc.status != 401:
                    raise ClientError(
                        f"{exc}\nthe login is kept; `titan logout --local` forgets it anyway",
                        exc.status,
                    ) from None
        self.config.unlink(missing_ok=True)
        self.say(f"logged out from {login.server}")
        return 0

    async def whoami(self, args: argparse.Namespace) -> int:
        login = self.login_or_fail()
        async with self.api(login) as api:
            me = await api.call("GET", "/me")
        print(f"{me['username']} ({me['role']}) on {login.server}", file=self.out)
        return 0

    # ------------------------------------------------------------ chat

    async def chat(self, args: argparse.Namespace) -> int:
        from pydantic import TypeAdapter

        from titan.api.chat import ChatEvent

        login = self.login_or_fail()
        message = self.stdin.read() if args.message == "-" else args.message
        if not message.strip():
            raise ClientError("the message is empty")
        events: TypeAdapter[Any] = TypeAdapter(ChatEvent)
        async with self.api(login) as api:
            if args.thread is not None:
                thread_id = args.thread
            elif args.continue_:
                if login.last_thread is None:
                    raise ClientError("no earlier thread here; start one with `titan chat`")
                thread_id = login.last_thread
            else:
                thread_id = uuid.UUID((await api.call("POST", "/chat/threads"))["id"])
                self.say(f"thread {thread_id}")
            if thread_id != login.last_thread:
                login.with_thread(thread_id).save(self.config)
            streamed = False
            async for raw in api.stream(
                f"/chat/threads/{thread_id}/messages", {"content": message}
            ):
                event = events.validate_json(raw.data)
                if event.type == "text":
                    self.out.write(event.delta)
                    self.out.flush()
                    streamed = streamed or bool(event.delta)
                elif event.type == "tool" and event.status != "finished":
                    self.say(f"[{event.name} {event.status}]")
                elif event.type == "approval":
                    approval = event.approval
                    self.say(
                        f"approval needed: {approval.summary}\n"
                        f"  titan approvals approve {approval.id}\n"
                        f"  titan approvals reject {approval.id}"
                    )
                elif event.type == "done":
                    if not streamed:
                        self.out.write(event.message.content)
                    self.out.write("\n")
                    return 0
                elif event.type == "error":
                    if streamed:
                        self.out.write("\n")
                    raise ClientError(f"{event.message.error or 'the reply failed'}")
        raise ClientError("the reply ended early; it is still written on the node")

    # ------------------------------------------------------- approvals

    async def approvals(self, args: argparse.Namespace) -> int:
        login = self.login_or_fail()
        async with self.api(login) as api:
            if args.approvals_command == "list":
                pending = await api.call("GET", "/approvals", pending="true")
                for item in pending:
                    print(
                        f"{item['id']}\t{item['domain']}\t{item['tool']}\t{item['summary']}",
                        file=self.out,
                    )
                if not pending:
                    self.say("no pending approvals")
                return 0
            verb = "approve" if args.approvals_command == "approve" else "reject"
            decided = await api.call("POST", f"/approvals/{args.approval_id}/{verb}")
        result = f": {decided['result']}" if decided.get("result") else ""
        print(f"{decided['status']}{result}", file=self.out)
        return 0 if decided["status"] != "failed" else 1


def add_parsers(sub: Any) -> None:
    """The client subcommands, added to `titan`'s subparsers."""
    li = sub.add_parser("login", help="pair this machine with a node as a cli device")
    li.add_argument("server", help="the node's base URL, such as http://titan-home:8000")
    li.add_argument("username")
    li.add_argument("--device-name", help="how the device is listed; the host name by default")
    li.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    lo = sub.add_parser("logout", help="revoke this machine's device and forget the login")
    lo.add_argument("--local", action="store_true", help="only forget the saved login")
    sub.add_parser("whoami", help="the saved server and account")
    ch = sub.add_parser("chat", help="send a message and stream the reply")
    ch.add_argument("message", help="the message, or - to read it from standard input")
    where = ch.add_mutually_exclusive_group()
    where.add_argument(
        "--continue",
        dest="continue_",
        action="store_true",
        help="continue the last thread used here",
    )
    where.add_argument("--thread", type=uuid.UUID, help="continue this thread")
    ap = sub.add_parser("approvals", help="answer the agent's approval requests")
    asub = ap.add_subparsers(dest="approvals_command", required=True)
    asub.add_parser("list", help="pending requests")
    for verb in ("approve", "reject"):
        one = asub.add_parser(verb, help=f"{verb} a request")
        one.add_argument("approval_id", type=uuid.UUID)


CLIENT_COMMANDS = frozenset({"login", "logout", "whoami", "chat", "approvals"})


async def run(commands: ClientCommands, args: argparse.Namespace) -> int:
    handler = {
        "login": commands.login,
        "logout": commands.logout,
        "whoami": commands.whoami,
        "chat": commands.chat,
        "approvals": commands.approvals,
    }[args.command]
    try:
        return await handler(args)
    except ClientError as exc:
        print(f"error: {exc}", file=commands.err)
    return 1
