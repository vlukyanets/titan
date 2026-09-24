"""Policy, approvals and the audit log over the API, with the M1 exit scenario."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from tests.fake_claude import FakeClaude
from tests.test_accounts_api import MEMBER_PW, OWNER_PW, Api, auth
from tests.test_agent_node import API_KEY
from tests.test_chat_api import parse_sse
from tests.test_notifications_api import FakePusher
from titan.agent.runtime import ChatRuntime
from titan.api.app import create_app
from titan.domains.accounts.models import Role
from titan.domains.autonomy.models import Approval
from titan.settings import Settings

pytestmark = pytest.mark.db

NOTIFY = "mcp__notifications__notify_member"
RENAME = "mcp__chat__rename_thread"


class AutonomyApi(Api):
    def claude(self, *calls: tuple[str, dict[str, Any]], reply: str = "Done.") -> FakeClaude:
        fake = FakeClaude(list(calls), reply)
        self.app.state.chat_runtime = ChatRuntime(
            self.app.state.settings,
            self.app.state.sessions,
            query_fn=fake,
            environ={"ANTHROPIC_API_KEY": API_KEY, "PATH": "/usr/bin"},
            pusher=self.app.state.pusher,
        )
        return fake

    async def chat(self, token: str, content: str) -> tuple[str, list[tuple[str, Any]]]:
        thread = await self.client.post("/api/v1/chat/threads", headers=auth(token))
        thread_id = thread.json()["id"]
        response = await self.client.post(
            f"/api/v1/chat/threads/{thread_id}/messages",
            headers=auth(token),
            json={"content": content},
        )
        assert response.status_code == 200, response.text
        return thread_id, parse_sse(response.text)

    async def get(self, token: str, path: str, **params: Any) -> Any:
        response = await self.client.get(f"/api/v1{path}", headers=auth(token), params=params)
        return response

    async def post(self, token: str, path: str) -> Any:
        return await self.client.post(f"/api/v1{path}", headers=auth(token))


@pytest.fixture
async def api(db_url: str, tmp_path: Path) -> AsyncIterator[AutonomyApi]:
    settings = Settings(
        database_url=db_url,
        claude_config_dir=tmp_path / "claude",
        push_allowed_origins=("http://push.test",),
    )
    app = create_app(settings)
    app.state.pusher = FakePusher()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield AutonomyApi(client, app)
    await app.state.chat_runtime.aclose()
    await app.state.engine.dispose()


async def tokens(api: AutonomyApi) -> tuple[str, str]:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", MEMBER_PW)
    return await api.token("anna", OWNER_PW), await api.token("boris", MEMBER_PW)


async def test_the_agent_asks_and_an_approval_runs_the_call(api: AutonomyApi) -> None:
    anna, boris = await tokens(api)
    api.claude(
        (NOTIFY, {"username": "boris", "message": "Buy milk"}),
        reply="I asked you to confirm the message to Boris.",
    )
    thread_id, events = await api.chat(anna, "Tell Boris to buy milk")
    names = [name for name, _ in events]
    assert names[0] == "turn"
    assert "approval" in names
    assert names[-1] == "done"
    approval = next(data for name, data in events if name == "approval")["approval"]
    assert approval["summary"] == "Send boris a notification: Buy milk"
    assert approval["status"] == "pending"
    assert approval["thread_id"] == thread_id

    # Anna's phone gets an approval notification that carries only the id.
    inbox = (await api.get(anna, "/notifications")).json()
    assert [(n["kind"], n["data"]) for n in inbox] == [
        ("approval", {"approval_id": approval["id"]})
    ]
    assert (await api.get(boris, "/notifications")).json() == []
    pending = (await api.get(anna, "/approvals", pending="true")).json()
    assert [a["id"] for a in pending] == [approval["id"]]

    # Boris cannot see or decide Anna's request.
    assert (await api.get(boris, f"/approvals/{approval['id']}")).status_code == 404
    assert (await api.post(boris, f"/approvals/{approval['id']}/approve")).status_code == 404

    approved = await api.post(anna, f"/approvals/{approval['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "executed"
    assert approved.json()["result"].startswith("Sent to boris")
    again = await api.post(anna, f"/approvals/{approval['id']}/approve")
    assert again.status_code == 409

    received = (await api.get(boris, "/notifications")).json()
    assert [(n["title"], n["body"]) for n in received] == [("From anna", "Buy milk")]
    messages = (await api.get(anna, f"/chat/threads/{thread_id}/messages")).json()
    assert messages[0]["content"].startswith("Approved: Send boris a notification")
    audit = (await api.get(anna, "/audit")).json()
    assert [(e["tool"], e["decision"], e["undoable"]) for e in audit] == [
        (NOTIFY, "confirm", False)
    ]
    assert (await api.post(anna, f"/audit/{audit[0]['id']}/undo")).status_code == 409


async def test_rejected_and_expired_requests_never_run(api: AutonomyApi) -> None:
    anna, boris = await tokens(api)
    api.claude(
        (NOTIFY, {"username": "boris", "message": "one"}),
        (NOTIFY, {"username": "boris", "message": "two"}),
    )
    await api.chat(anna, "Send Boris two messages")
    first, second = (await api.get(anna, "/approvals")).json()
    rejected = await api.post(anna, f"/approvals/{first['id']}/reject")
    assert rejected.json()["status"] == "rejected"
    assert (await api.post(anna, f"/approvals/{first['id']}/approve")).status_code == 409

    await api.execute(
        update(Approval).where(Approval.id == second["id"]).values(expires_at=Approval.created_at)
    )
    expired = await api.post(anna, f"/approvals/{second['id']}/approve")
    assert expired.status_code == 409
    assert "expired" in expired.json()["detail"]
    assert (await api.get(boris, "/notifications")).json() == []


async def test_rules_change_what_the_agent_may_do(api: AutonomyApi) -> None:
    anna, boris = await tokens(api)
    rules = (await api.get(boris, "/policy")).json()
    notify_rule = next(
        r for r in rules if r["domain"] == "notifications" and r["action_class"] == "external"
    )
    assert (notify_rule["decision"], notify_rule["source"]) == ("confirm", "default")

    forbidden = await api.client.put(
        "/api/v1/policy/household/notifications/external",
        headers=auth(boris),
        json={"decision": "auto"},
    )
    assert forbidden.status_code == 403
    unknown = await api.client.put(
        "/api/v1/policy/weather/read", headers=auth(boris), json={"decision": "auto"}
    )
    assert unknown.status_code == 422

    household = await api.client.put(
        "/api/v1/policy/household/notifications/external",
        headers=auth(anna),
        json={"decision": "deny"},
    )
    assert household.status_code == 204
    own = await api.client.put(
        "/api/v1/policy/notifications/external", headers=auth(boris), json={"decision": "auto"}
    )
    assert own.status_code == 204
    rules = {(r["domain"], r["action_class"]): r for r in (await api.get(boris, "/policy")).json()}
    assert rules[("notifications", "external")]["source"] == "user"
    anna_rules = (await api.get(anna, "/policy")).json()
    assert {
        (r["decision"], r["source"])
        for r in anna_rules
        if r["domain"] == "notifications" and r["action_class"] == "external"
    } == {("deny", "household")}

    # With `auto`, Boris's agent sends without asking.
    api.claude((NOTIFY, {"username": "anna", "message": "Home soon"}))
    await api.chat(boris, "Tell Anna I'm home soon")
    assert (await api.get(boris, "/approvals")).json() == []
    assert [n["body"] for n in (await api.get(anna, "/notifications")).json()] == ["Home soon"]

    removed = await api.client.delete("/api/v1/policy/notifications/external", headers=auth(boris))
    assert removed.status_code == 204


async def test_undo_from_the_audit_log(api: AutonomyApi) -> None:
    anna, boris = await tokens(api)
    api.claude((RENAME, {"title": "Weekend trip"}))
    thread_id, _ = await api.chat(anna, "Call this conversation Weekend trip")
    thread = (await api.get(anna, f"/chat/threads/{thread_id}")).json()
    assert thread["title"] == "Weekend trip"
    (entry,) = (await api.get(anna, "/audit")).json()
    assert entry["undoable"]
    assert entry["before"] == {"title": "Call this conversation Weekend trip"}
    assert (await api.post(boris, f"/audit/{entry['id']}/undo")).status_code == 404

    undone = await api.post(anna, f"/audit/{entry['id']}/undo")
    assert undone.status_code == 200
    assert undone.json()["undone_at"] is not None
    assert undone.json()["undoable"] is False
    thread = (await api.get(anna, f"/chat/threads/{thread_id}")).json()
    assert thread["title"] == "Call this conversation Weekend trip"
    assert (await api.post(anna, f"/audit/{entry['id']}/undo")).status_code == 409


async def test_undo_refuses_after_a_later_change(api: AutonomyApi) -> None:
    anna, _ = await tokens(api)
    api.claude((RENAME, {"title": "First"}), (RENAME, {"title": "Second"}))
    await api.chat(anna, "Rename twice")
    second, first = (await api.get(anna, "/audit")).json()
    conflict = await api.post(anna, f"/audit/{first['id']}/undo")
    assert conflict.status_code == 409
    assert "renamed again" in conflict.json()["detail"]
    assert (await api.post(anna, f"/audit/{second['id']}/undo")).status_code == 200
    assert (await api.post(anna, f"/audit/{first['id']}/undo")).status_code == 200
