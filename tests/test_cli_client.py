"""The client commands of `titan` against the real app: login, chat, approvals."""

from __future__ import annotations

import io
import json
import stat
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from tests.test_accounts_api import MEMBER_PW, OWNER_PW
from tests.test_agent_node import init, result
from tests.test_autonomy_api import NOTIFY, AutonomyApi, tokens
from tests.test_chat_api import ChatApi
from tests.test_chat_turn import delta
from tests.test_notifications_api import FakePusher
from titan.api.app import create_app
from titan.cli.client import ClientError, Login, ServerEvent, server_events
from titan.cli.commands import ClientCommands, run
from titan.cli.main import main, parser
from titan.domains.accounts.models import Role
from titan.settings import Settings

SERVER = "http://test"


async def lines(*items: str) -> AsyncIterator[str]:
    for item in items:
        yield item


async def test_server_events_follow_the_event_stream_format() -> None:
    events = [
        e
        async for e in server_events(
            lines(": ping", "event: text", 'data: {"a":', "data: 1}", "", "data: x", "")
        )
    ]
    assert events == [ServerEvent("text", '{"a":\n1}'), ServerEvent("message", "x")]
    trailing = [e async for e in server_events(lines("event: done", "data:{}"))]
    assert trailing == [ServerEvent("done", "{}")]


def test_client_commands_need_a_login(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["whoami"], client_config=tmp_path / "client.json") == 1
    assert "not logged in: run `titan login SERVER USERNAME`" in capsys.readouterr().err
    login = ["login", "titan-home:8000", "anna", "--password-stdin"]
    assert main(login, client_config=tmp_path / "c.json", stdin=io.StringIO("pw\n")) == 1
    assert "must be a URL" in capsys.readouterr().err


def test_the_saved_login_is_private(tmp_path: Path) -> None:
    path = tmp_path / "config" / "titan" / "client.json"
    login = Login(SERVER, "anna", uuid.uuid4(), "secret-token")
    login.save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert Login.load(path) == login
    assert list(path.parent.iterdir()) == [path]
    path.write_text("{")
    with pytest.raises(ClientError, match="unreadable"):
        Login.load(path)


@pytest.fixture
async def chat_api(db_url: str, tmp_path: Path) -> AsyncIterator[ChatApi]:
    app = create_app(Settings(database_url=db_url, claude_config_dir=tmp_path / "claude"))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=SERVER) as client:
        yield ChatApi(client, app)
    await app.state.chat_runtime.aclose()
    await app.state.engine.dispose()


@pytest.fixture
async def autonomy_api(db_url: str, tmp_path: Path) -> AsyncIterator[AutonomyApi]:
    settings = Settings(
        database_url=db_url,
        claude_config_dir=tmp_path / "claude",
        push_allowed_origins=("http://push.test",),
    )
    app = create_app(settings)
    app.state.pusher = FakePusher()
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=SERVER) as client:
        yield AutonomyApi(client, app)
    await app.state.chat_runtime.aclose()
    await app.state.engine.dispose()


@dataclass
class Titan:
    app: object
    config: Path

    async def __call__(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        commands = ClientCommands(
            config=self.config,
            stdin=io.StringIO(stdin),
            out=out,
            err=err,
            transport=ASGITransport(app=self.app),  # type: ignore[arg-type]
        )
        code = await run(commands, parser().parse_args(list(argv)))
        return code, out.getvalue(), err.getvalue()


@pytest.mark.db
async def test_login_chat_and_logout(chat_api: ChatApi, tmp_path: Path) -> None:
    await chat_api.create_user("anna", OWNER_PW, Role.OWNER)
    titan = Titan(chat_api.app, tmp_path / "titan" / "client.json")

    code, _, err = await titan("login", SERVER, "anna", "--password-stdin", stdin="wrong\n")
    assert code == 1
    assert "error: " in err
    assert not titan.config.exists()

    code, _, err = await titan(
        "login",
        SERVER + "/",
        "anna",
        "--device-name",
        "laptop",
        "--password-stdin",
        stdin=f"{OWNER_PW}\n",
    )
    assert code == 0, err
    assert f"logged in to {SERVER} as anna" in err
    saved = json.loads(titan.config.read_text())
    assert saved["server"] == SERVER
    assert OWNER_PW not in titan.config.read_text()
    assert stat.S_IMODE(titan.config.stat().st_mode) == 0o600

    assert await titan("whoami") == (0, f"anna (owner) on {SERVER}\n", "")

    code, _, err = await titan("chat", "--continue", "hello")
    assert (code, err) == (1, "error: no earlier thread here; start one with `titan chat`\n")

    fake = chat_api.answer_with(init(), delta("Hel"), delta("lo!"), result(result="Hello!"))
    code, out, err = await titan("chat", "hello")
    assert (code, out) == (0, "Hello!\n"), err
    thread_id = err.split()[1]
    assert Login.load(titan.config).last_thread == uuid.UUID(thread_id)

    code, out, _ = await titan("chat", "--continue", "-", stdin="and again")
    assert (code, out) == (0, "Hello!\n")
    prompt, _ = fake.calls[-1]
    assert "hello" in prompt
    assert prompt.endswith("and again")

    # Logging in again revokes the device the earlier login held.
    old_token = Login.load(titan.config).token
    code, _, err = await titan("login", SERVER, "anna", "--password-stdin", stdin=f"{OWNER_PW}\n")
    assert code == 0
    assert "revoked the previous device" in err
    refused = await chat_api.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {old_token}"}
    )
    assert refused.status_code == 401

    token = Login.load(titan.config).token
    code, _, err = await titan("logout")
    assert (code, err) == (0, f"logged out from {SERVER}\n")
    assert not titan.config.exists()
    gone = await chat_api.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert gone.status_code == 401
    code, _, err = await titan("logout")
    assert code == 1
    assert "not logged in" in err


@pytest.mark.db
async def test_a_revoked_login_explains_itself(chat_api: ChatApi, tmp_path: Path) -> None:
    await chat_api.create_user("boris", MEMBER_PW)
    titan = Titan(chat_api.app, tmp_path / "client.json")
    await titan("login", SERVER, "boris", "--password-stdin", stdin=f"{MEMBER_PW}\n")
    login = Login.load(titan.config)
    revoke = await chat_api.client.delete(
        f"/api/v1/devices/{login.device_id}", headers={"Authorization": f"Bearer {login.token}"}
    )
    assert revoke.status_code == 204
    code, _, err = await titan("whoami")
    assert code == 1
    assert "the device may have been revoked; `titan login` pairs it again" in err
    # The server already refuses the token, so logout just forgets it.
    assert (await titan("logout"))[0] == 0
    assert not titan.config.exists()


async def test_an_unreachable_server_keeps_the_login(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    config = tmp_path / "client.json"
    Login(SERVER, "anna", uuid.uuid4(), "token").save(config)
    out, err = io.StringIO(), io.StringIO()
    commands = ClientCommands(
        config=config, stdin=io.StringIO(), out=out, err=err, transport=httpx.MockTransport(refuse)
    )
    assert await run(commands, parser().parse_args(["logout"])) == 1
    assert "cannot reach http://test/api/v1/ (ConnectError)" in err.getvalue()
    assert "`titan logout --local` forgets it anyway" in err.getvalue()
    assert config.exists()
    assert await run(commands, parser().parse_args(["logout", "--local"])) == 0
    assert not config.exists()


@pytest.mark.db
async def test_an_approval_asked_in_chat_is_answered_from_the_cli(
    autonomy_api: AutonomyApi,
    tmp_path: Path,
) -> None:
    await tokens(autonomy_api)
    titan = Titan(autonomy_api.app, tmp_path / "client.json")
    await titan("login", SERVER, "anna", "--password-stdin", stdin=f"{OWNER_PW}\n")
    autonomy_api.claude(
        (NOTIFY, {"username": "boris", "message": "Buy milk"}),
        reply="I asked you to confirm the message to Boris.",
    )
    code, out, err = await titan("chat", "Tell Boris to buy milk")
    assert code == 0, err
    assert out == "I asked you to confirm the message to Boris.\n"
    assert "approval needed: Send boris a notification: Buy milk" in err
    approval_id = err.split("titan approvals approve ")[1].split()[0]

    code, out, _ = await titan("approvals", "list")
    assert out.startswith(f"{approval_id}\tnotifications\t{NOTIFY}\tSend boris")
    code, out, _ = await titan("approvals", "approve", approval_id)
    assert code == 0
    assert out.startswith("executed: Sent to boris")
    code, _, err = await titan("approvals", "reject", approval_id)
    assert code == 1
    assert err.startswith("error: ")
    assert await titan("approvals", "list") == (0, "", "no pending approvals\n")
