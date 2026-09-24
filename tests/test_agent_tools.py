"""Domain tools, the policy hook and approvals, through a CLI-like fake Claude."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.fake_claude import FakeClaude
from tests.test_agent_node import API_KEY
from tests.test_notifications_api import FakePusher
from titan.agent.chat import ApprovalRequested
from titan.agent.runtime import ChatRuntime, TurnEnded
from titan.agent.tools import REGISTRY, ToolScope, execute, mcp_servers, run_approved
from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService, Principal
from titan.domains.autonomy.models import (
    ActionClass,
    Approval,
    ApprovalStatus,
    AuditEntry,
    Decision,
)
from titan.domains.autonomy.service import ApprovalsService, AuditService, PolicyService
from titan.domains.chat.models import ChatMessage, ChatThread, MessageRole
from titan.domains.chat.service import ChatService
from titan.domains.notifications.models import Notification, NotificationKind
from titan.settings import Settings

RENAME = "mcp__chat__rename_thread"
NOTIFY = "mcp__notifications__notify_member"
MEMBERS = "mcp__accounts__list_members"

EXAMPLES: dict[str, dict[str, Any]] = {
    RENAME: {"title": "Weekend trip"},
    NOTIFY: {"username": "boris", "message": "Buy milk"},
    MEMBERS: {},
}


def test_every_tool_is_declared_completely() -> None:
    assert set(REGISTRY) == set(EXAMPLES), "add an example for every new tool"
    for name, spec in REGISTRY.items():
        assert spec.qualified_name == name
        assert isinstance(spec.action_class, ActionClass)
        jsonschema.Draft202012Validator.check_schema(spec.input_schema)
        jsonschema.validate(EXAMPLES[name], spec.input_schema)
        assert spec.summarize(EXAMPLES[name])
        if spec.action_class is ActionClass.WRITE_INTERNAL:
            assert spec.undo is not None, f"{name} is write-internal but cannot be undone"


# ------------------------------------------------------------------ with a database


class World:
    def __init__(self, db_url: str, tmp_path: Path) -> None:
        self.settings = Settings(
            database_url=db_url,
            claude_config_dir=tmp_path / "claude",
            push_allowed_origins=("http://push.test",),
        )
        self.engine = create_async_engine(db_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.pusher = FakePusher()
        self.environ = {"ANTHROPIC_API_KEY": API_KEY, "PATH": "/usr/bin"}

    async def setup(self) -> None:
        async with self.sessions() as session:
            accounts = AccountsService(session)
            anna = await accounts.create_user(
                "anna", "a-long-test-password", role=Role.OWNER, display_name="Anna"
            )
            boris = await accounts.create_user(
                "boris", "a-long-test-password", display_name="Boris"
            )
        self.anna = Principal(anna.id, anna.username, anna.role, uuid.uuid4())
        self.boris = Principal(boris.id, boris.username, boris.role, uuid.uuid4())
        async with self.sessions() as session:
            self.thread = await ChatService(session).create_thread(self.anna, "Old title")

    async def turn(self, fake: FakeClaude, content: str = "please") -> list[Any]:
        async with self.sessions() as session:
            turn = await ChatService(session).start_turn(self.anna, self.thread.id, content)
        runtime = ChatRuntime(
            self.settings,
            self.sessions,
            query_fn=fake,
            environ=self.environ,
            pusher=self.pusher,
        )
        try:
            return [e async for e in runtime.start(self.anna.user_id, turn.assistant_message.id)]
        finally:
            await runtime.aclose()

    def scope(self, thread_id: uuid.UUID | None = None) -> ToolScope:
        return ToolScope(
            self.sessions,
            self.anna.user_id,
            thread_id or self.thread.id,
            self.pusher,
            self.settings.push_allowed_origins,
        )

    async def rows(self, model: Any, *where: Any) -> list[Any]:
        async with self.sessions() as session:
            return list((await session.scalars(select(model).where(*where))).all())

    async def title(self) -> str:
        async with self.sessions() as session:
            thread = await session.get(ChatThread, self.thread.id)
            assert thread is not None
            return thread.title


@pytest.fixture
async def world(db_url: str, tmp_path: Path) -> AsyncIterator[World]:
    w = World(db_url, tmp_path)
    await w.setup()
    yield w
    await w.engine.dispose()


@pytest.mark.db
async def test_auto_undo_runs_is_audited_and_can_be_undone(world: World) -> None:
    fake = FakeClaude([(RENAME, {"title": "Weekend trip"})])
    events = await world.turn(fake)
    assert fake.options[0].allowed_tools == []
    (outcome,) = fake.outcomes
    assert (outcome.decision, outcome.is_error) == ("allow", False)
    assert "Weekend trip" in (outcome.result or "")
    assert await world.title() == "Weekend trip"
    assert isinstance(events[-1], TurnEnded)

    (entry,) = await world.rows(AuditEntry)
    assert (entry.tool, entry.decision) == (RENAME, Decision.AUTO_UNDO)
    assert (entry.before, entry.after) == ({"title": "Old title"}, {"title": "Weekend trip"})
    assert entry.undoable
    async with world.sessions() as session:
        await AuditService(session).undo(
            world.anna, entry.id, REGISTRY, world.scope().context(session)
        )
    assert await world.title() == "Old title"


@pytest.mark.db
async def test_confirm_stores_a_request_and_runs_nothing(world: World) -> None:
    fake = FakeClaude([(NOTIFY, {"username": "boris", "message": "Buy milk"})])
    events = await world.turn(fake)
    (outcome,) = fake.outcomes
    assert outcome.decision == "deny"
    assert "Send boris a notification: Buy milk" in outcome.reason
    assert outcome.result is None

    (approval,) = await world.rows(Approval)
    assert approval.status is ApprovalStatus.PENDING
    assert approval.input == {"username": "boris", "message": "Buy milk"}
    assert approval.thread_id == world.thread.id
    assert [e.approval.id for e in events if isinstance(e, ApprovalRequested)] == [approval.id]
    # Anna is asked; nothing reached Boris yet.
    (asked,) = await world.rows(Notification)
    assert (asked.user_id, asked.kind) == (world.anna.user_id, NotificationKind.APPROVAL)
    assert asked.data == {"approval_id": str(approval.id)}
    assert await world.rows(AuditEntry) == []

    async with world.sessions() as session:
        claimed = await ApprovalsService(session).claim(world.anna, approval.id)
    done = await run_approved(claimed, world.scope())
    assert done.status is ApprovalStatus.EXECUTED
    assert done.result is not None
    assert done.result.startswith("Sent to Boris")
    sent = await world.rows(Notification, Notification.user_id == world.boris.user_id)
    assert [(n.title, n.body) for n in sent] == [("From Anna", "Buy milk")]
    (entry,) = await world.rows(AuditEntry)
    assert (entry.approval_id, entry.decision, entry.undoable) == (
        approval.id,
        Decision.CONFIRM,
        False,
    )
    notes = await world.rows(
        ChatMessage,
        ChatMessage.thread_id == world.thread.id,
        ChatMessage.role == MessageRole.ASSISTANT,
        ChatMessage.model.is_(None),
    )
    assert [n.content.splitlines()[0] for n in notes] == [
        "Approved: Send boris a notification: Buy milk"
    ]


@pytest.mark.db
async def test_deny_rules_and_unknown_tools_are_refused(world: World) -> None:
    async with world.sessions() as session:
        await PolicyService(session).set_rule(
            world.anna, "chat", ActionClass.WRITE_INTERNAL, Decision.DENY
        )
    fake = FakeClaude([(RENAME, {"title": "Nope"}), ("Bash", {"command": "ls"})])
    await world.turn(fake)
    denied, unknown = fake.outcomes
    assert denied.decision == "deny"
    assert "does not allow write-internal actions in chat" in denied.reason
    assert unknown.decision == "deny"
    assert "not a TITAN tool" in unknown.reason
    assert await world.title() == "Old title"
    assert await world.rows(AuditEntry) == []


@pytest.mark.db
async def test_reads_are_not_audited(world: World) -> None:
    fake = FakeClaude([(MEMBERS, {})])
    await world.turn(fake)
    (outcome,) = fake.outcomes
    assert outcome.decision == "allow"
    assert "anna (Anna) (you)" in (outcome.result or "")
    assert "boris (Boris)" in (outcome.result or "")
    assert await world.rows(AuditEntry) == []


@pytest.mark.db
async def test_the_tool_wrapper_refuses_calls_that_skipped_the_hook(world: World) -> None:
    # A session without the policy hook: Claude Code's permission check would
    # refuse, and even if a call got through, the wrapper refuses confirm tools.
    servers = mcp_servers(world.scope())
    fake = FakeClaude([(NOTIFY, {"username": "boris", "message": "Buy milk"})])
    options = ClaudeAgentOptions(mcp_servers=servers, allowed_tools=[NOTIFY])
    async for _ in fake(prompt="x", options=options):
        pass
    (outcome,) = fake.outcomes
    assert outcome.decision == "allow"
    assert outcome.is_error
    assert "not allowed without the user's approval" in (outcome.result or "")
    assert await world.rows(Notification) == []


@pytest.mark.db
async def test_invalid_input_changes_nothing(world: World) -> None:
    spec = REGISTRY[RENAME]
    execution = await execute(spec, world.scope(), {"title": ""}, decision=Decision.AUTO_UNDO)
    assert execution.result.is_error
    assert "Invalid input" in execution.result.text
    assert await world.title() == "Old title"
    async with world.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(AuditEntry))
    assert count == 0


@pytest.mark.db
async def test_a_rename_outside_a_thread_fails_cleanly(world: World) -> None:
    scope = ToolScope(world.sessions, world.anna.user_id, None)
    execution = await execute(REGISTRY[RENAME], scope, {"title": "x"}, decision=Decision.AUTO_UNDO)
    assert execution.result.is_error
    assert execution.audit_entry_id is None
