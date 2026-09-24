"""Usage over the API and the node command."""

from __future__ import annotations

import io
import uuid
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


async def test_the_owner_caps_a_member_and_the_member_sees_it(api: ChatApi) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    boris_id = (await api.create_user("boris", MEMBER_PW)).id
    anna = await api.token("anna", OWNER_PW)
    boris = await api.token("boris", MEMBER_PW)
    api.answer_with(init(), delta("Hi"), result(result="Hi!"))
    thread = await api.thread(boris)
    assert (await api.send(boris, thread["id"], "hello")).status_code == 200

    uncapped = (await api.client.get("/api/v1/usage/budget", headers=auth(boris))).json()
    assert uncapped == {
        "month": current_month(),
        "limit_usd": None,
        "spent_usd": pytest.approx(0.0123),
        "state": "ok",
    }
    url = f"/api/v1/usage/budgets/{boris_id}"
    capped = await api.client.put(url, headers=auth(anna), json={"limit_usd": 0.005})
    assert capped.status_code == 200
    assert (capped.json()["username"], capped.json()["limit_usd"]) == ("boris", 0.01)
    assert capped.json()["state"] == "exceeded"
    mine = (await api.client.get("/api/v1/usage/budget", headers=auth(boris))).json()
    assert (mine["limit_usd"], mine["state"]) == (0.01, "exceeded")
    household = (await api.client.get("/api/v1/usage/budgets", headers=auth(anna))).json()
    assert [(m["username"], m["limit_usd"]) for m in household] == [
        ("anna", None),
        ("boris", 0.01),
    ]
    notifications = (await api.client.get("/api/v1/notifications", headers=auth(boris))).json()
    assert [n["kind"] for n in notifications] == ["budget"]

    for who, body, status in [
        (boris, {"limit_usd": 100}, 403),
        (anna, {"limit_usd": -1}, 422),
        (anna, {"limit_usd": 100_001}, 422),
        (anna, {}, 422),
    ]:
        refused = await api.client.put(url, headers=auth(who), json=body)
        assert refused.status_code == status
        assert refused.headers["content-type"] == "application/problem+json"
    assert (await api.client.get("/api/v1/usage/budgets", headers=auth(boris))).status_code == 403
    unknown = f"/api/v1/usage/budgets/{uuid.uuid4()}"
    missing = await api.client.put(unknown, headers=auth(anna), json={"limit_usd": 1})
    assert missing.status_code == 404
    removed = await api.client.put(url, headers=auth(anna), json={"limit_usd": None})
    assert (removed.json()["limit_usd"], removed.json()["state"]) == (None, "ok")


def test_the_node_command_sets_budgets(db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(database_url=db_url)
    password = io.StringIO("owner-password-123\n")
    create = ["users", "create", "anna", "--owner", "--password-stdin"]
    assert main(create, settings=settings, stdin=password) == 0
    capsys.readouterr()
    assert main(["budget", "set", "anna", "25.5"], settings=settings) == 0
    assert (
        capsys.readouterr().out == f"anna: 25.50 USD a month, 0.00 spent in {current_month()}, ok\n"
    )
    assert main(["budget", "list"], settings=settings) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"{current_month()}\tlimit USD\tspent USD\tstate", "anna\t25.50\t0.0000\tok"]
    assert main(["budget", "set", "anna", "none"], settings=settings) == 0
    assert capsys.readouterr().out == "anna: no limit\n"
    assert main(["budget", "set", "anna", "lots"], settings=settings) == 1
    assert "not an amount" in capsys.readouterr().err
    assert main(["budget", "set", "anna", "-3"], settings=settings) == 1
    assert "between 0 and" in capsys.readouterr().err
    assert main(["budget", "set", "nobody", "3"], settings=settings) == 1
