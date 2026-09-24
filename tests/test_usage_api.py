"""Usage over the API and the node command."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_accounts_api import MEMBER_PW, OWNER_PW, auth
from tests.test_agent_node import init, result
from tests.test_chat_api import ChatApi
from tests.test_chat_turn import delta
from titan.api.app import create_app
from titan.cli.main import main
from titan.domains.accounts.models import Role
from titan.domains.usage.service import current_month
from titan.settings import Settings

pytestmark = pytest.mark.db


@pytest.fixture
async def api(db_url: str, tmp_path: Path) -> AsyncIterator[ChatApi]:
    settings = Settings(
        database_url=db_url,
        claude_config_dir=tmp_path / "claude",
        claude_model_strong="strong-model",
    )
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield ChatApi(client, app)
    await app.state.chat_runtime.aclose()
    await app.state.engine.dispose()


async def test_a_chat_turn_shows_up_in_usage(api: ChatApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", MEMBER_PW)
    anna = await api.token("anna", OWNER_PW)
    boris = await api.token("boris", MEMBER_PW)
    api.answer_with(init(), delta("Hi"), result(result="Hi!"))
    thread = await api.thread(anna)
    assert (await api.send(anna, thread["id"], "hello")).status_code == 200

    mine = (await api.client.get("/api/v1/usage", headers=auth(anna))).json()
    assert mine["month"] == current_month()
    assert mine["total"]["sessions"] == 1
    assert (mine["total"]["input_tokens"], mine["total"]["output_tokens"]) == (120, 30)
    assert mine["total"]["cost_usd"] == pytest.approx(0.0123)
    assert [m["model"] for m in mine["by_model"]] == ["strong-model"]

    empty = (await api.client.get("/api/v1/usage", headers=auth(boris))).json()
    assert empty["total"]["sessions"] == 0
    assert empty["by_model"] == []
    earlier = await api.client.get("/api/v1/usage", headers=auth(anna), params={"month": "2020-01"})
    assert earlier.json()["total"]["sessions"] == 0

    household = await api.client.get("/api/v1/usage/household", headers=auth(anna))
    assert [(m["username"], m["total"]["sessions"]) for m in household.json()] == [
        ("anna", 1),
        ("boris", 0),
    ]
    forbidden = await api.client.get("/api/v1/usage/household", headers=auth(boris))
    assert forbidden.status_code == 403
    bad = await api.client.get("/api/v1/usage", headers=auth(anna), params={"month": "sept"})
    assert bad.status_code == 422
    assert bad.headers["content-type"] == "application/problem+json"


def test_the_node_command_lists_everyone(db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(database_url=db_url)
    password = io.StringIO("owner-password-123\n")
    create = ["users", "create", "anna", "--owner", "--password-stdin"]
    assert main(create, settings=settings, stdin=password) == 0
    capsys.readouterr()
    assert main(["usage"], settings=settings) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f"{current_month()}\tsessions")
    assert lines[1] == "anna\t0\t0\t0\t0\t0\t0.0000"
    assert main(["usage", "--month", "2026-13"], settings=settings) == 1
    assert "month must look like" in capsys.readouterr().err
