"""Chat routes: threads, messages, the SSE reply stream, ownership and refusals."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_accounts_api import MEMBER_PW, OWNER_PW, Api, auth
from tests.test_agent_node import API_KEY, FakeQuery, init, result
from tests.test_chat_turn import delta
from titan.agent.runtime import ChatRuntime
from titan.api.app import create_app
from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService, Principal
from titan.domains.chat.service import ChatService
from titan.settings import Settings

pytestmark = pytest.mark.db


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if not line.startswith(":")
        )
        if fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


class ChatApi(Api):
    fake: FakeQuery

    def answer_with(self, *messages: Any, environ: dict[str, str] | None = None) -> FakeQuery:
        settings: Settings = self.app.state.settings
        self.fake = FakeQuery(*messages)
        # The runtime's own self-check consumes one session; give it an init.
        checker = FakeQuery(init())
        runtime = ChatRuntime(
            settings,
            self.app.state.sessions,
            query_fn=lambda **kw: (checker if not checker.calls else self.fake)(**kw),
            environ=environ
            if environ is not None
            else {"ANTHROPIC_API_KEY": API_KEY, "PATH": "/usr/bin"},
        )
        self.app.state.chat_runtime = runtime
        return self.fake

    async def thread(self, token: str, title: str | None = None) -> dict[str, Any]:
        body = {"title": title} if title is not None else None
        response = await self.client.post("/api/v1/chat/threads", headers=auth(token), json=body)
        assert response.status_code == 201, response.text
        return dict(response.json())

    async def send(self, token: str, thread_id: str, content: str) -> Any:
        return await self.client.post(
            f"/api/v1/chat/threads/{thread_id}/messages",
            headers=auth(token),
            json={"content": content},
        )


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


async def tokens(api: ChatApi) -> tuple[str, str]:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", MEMBER_PW)
    return await api.token("anna", OWNER_PW), await api.token("boris", MEMBER_PW)


async def test_a_message_streams_its_reply(api: ChatApi) -> None:
    anna, _ = await tokens(api)
    api.answer_with(init(), delta("Two tasks "), delta("are due."), result(result="Two tasks."))
    thread = await api.thread(anna)
    assert thread["title"] == ""

    response = await api.send(anna, thread["id"], "What is due today?")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["turn", "text", "text", "done"]
    assert all(data["type"] == name for name, data in events)
    turn = events[0][1]
    assert turn["user_message"]["content"] == "What is due today?"
    assert turn["assistant_message"]["status"] == "streaming"
    assert events[1][1]["delta"] == "Two tasks "
    done = events[3][1]["message"]
    assert done["id"] == turn["assistant_message"]["id"]
    assert done["status"] == "complete"
    assert done["content"] == "Two tasks."
    assert done["model"] == "strong-model"
    assert done["usage"]["output_tokens"] == 30

    listed = await api.client.get(
        f"/api/v1/chat/threads/{thread['id']}/messages", headers=auth(anna)
    )
    assert [(m["role"], m["content"]) for m in listed.json()] == [
        ("assistant", "Two tasks."),
        ("user", "What is due today?"),
    ]
    got = await api.client.get(f"/api/v1/chat/threads/{thread['id']}", headers=auth(anna))
    assert got.json()["title"] == "What is due today?"


async def test_a_failed_reply_ends_with_an_error_event(api: ChatApi) -> None:
    anna, _ = await tokens(api)
    api.answer_with(init(), result(is_error=True, subtype="error_max_turns"))
    thread = await api.thread(anna)
    events = parse_sse((await api.send(anna, thread["id"], "hello")).text)
    assert [name for name, _ in events] == ["turn", "error"]
    failed = events[1][1]["message"]
    assert failed["status"] == "failed"
    assert failed["error"] == "the session failed: error_max_turns"
    assert failed["usage"] is None


async def test_one_reply_at_a_time(api: ChatApi) -> None:
    anna, _ = await tokens(api)
    api.answer_with(init(), result())
    thread = await api.thread(anna)
    async with api.app.state.sessions() as session:
        user = await AccountsService(session).get_user_by_username("anna")
        principal = Principal(user.id, user.username, user.role, uuid.uuid4())
        await ChatService(session).start_turn(principal, uuid.UUID(thread["id"]), "running")
    response = await api.send(anna, thread["id"], "another")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"


async def test_threads_are_private(api: ChatApi) -> None:
    anna, boris = await tokens(api)
    api.answer_with(init(), result())
    thread = await api.thread(anna, "Anna's plans")
    for method, path in [
        ("GET", f"/api/v1/chat/threads/{thread['id']}"),
        ("GET", f"/api/v1/chat/threads/{thread['id']}/messages"),
        ("DELETE", f"/api/v1/chat/threads/{thread['id']}"),
    ]:
        response = await api.client.request(method, path, headers=auth(boris))
        assert response.status_code == 404, path
    assert (await api.send(boris, thread["id"], "hi")).status_code == 404
    listed = await api.client.get("/api/v1/chat/threads", headers=auth(boris))
    assert listed.json() == []
    listed = await api.client.get("/api/v1/chat/threads", headers=auth(anna))
    assert [t["title"] for t in listed.json()] == ["Anna's plans"]
    deleted = await api.client.delete(f"/api/v1/chat/threads/{thread['id']}", headers=auth(anna))
    assert deleted.status_code == 204


async def test_refusals_are_problem_details(api: ChatApi) -> None:
    anna, _ = await tokens(api)
    api.answer_with(init(), result())
    thread = await api.thread(anna)
    assert (await api.send(anna, thread["id"], "   ")).status_code == 422
    assert (await api.send(anna, thread["id"], "x" * 8001)).status_code == 422
    unauthenticated = await api.client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages", json={"content": "hi"}
    )
    assert unauthenticated.status_code == 401


async def test_without_a_credential_chat_answers_503(api: ChatApi) -> None:
    anna, _ = await tokens(api)
    fake = api.answer_with(init(), result(), environ={"PATH": "/usr/bin"})
    thread = await api.thread(anna)
    response = await api.send(anna, thread["id"], "hello")
    assert response.status_code == 503
    assert "credential" in response.json()["detail"]
    assert fake.calls == []
    messages = await api.client.get(
        f"/api/v1/chat/threads/{thread['id']}/messages", headers=auth(anna)
    )
    assert messages.json() == []
