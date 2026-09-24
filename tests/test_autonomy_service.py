"""Policy resolution, approval states and expiry, audit entries and undo."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService, Principal
from titan.domains.autonomy.errors import (
    ApprovalClosedError,
    ForbiddenError,
    InvalidRuleError,
    NotFoundError,
    NotUndoableError,
    UndoConflictError,
)
from titan.domains.autonomy.models import (
    DOMAINS,
    ActionClass,
    Approval,
    ApprovalStatus,
    Decision,
)
from titan.domains.autonomy.service import ApprovalsService, AuditService, PolicyService
from titan.domains.autonomy.tools import Change, ToolContext, ToolResult, ToolSpec

pytestmark = pytest.mark.db


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def principal(
    sessions: async_sessionmaker[AsyncSession], username: str, role: Role = Role.MEMBER
) -> Principal:
    async with sessions() as session:
        user = await AccountsService(session).create_user(
            username, "a-long-test-password", role=role
        )
        return Principal(user.id, user.username, user.role, uuid.uuid4())


# A tool over an in-memory "entity" standing in for domain data.
STORE: dict[str, str] = {}


async def run_rename(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    before = STORE.get("thing", "")
    STORE["thing"] = str(args["title"])
    return ToolResult("renamed", Change("thing", "1", {"title": before}, {"title": args["title"]}))


async def undo_rename(context: ToolContext, change: Change) -> None:
    if STORE.get("thing") != change.after["title"]:
        raise UndoConflictError("the thing was renamed again")
    STORE["thing"] = str(change.before["title"])


def summarize(args: Mapping[str, Any]) -> str:
    return f"Rename the thing to {args['title']}"


RENAME = ToolSpec(
    domain="chat",
    name="rename_thing",
    description="Rename the thing",
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={"type": "object"},
    run=run_rename,
    summarize=summarize,
    undo=undo_rename,
)


async def test_rules_resolve_user_then_household_then_default(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    owner = await principal(sessions, "anna", Role.OWNER)
    member = await principal(sessions, "boris")
    async with sessions() as session:
        policy = PolicyService(session)
        external = ActionClass.EXTERNAL
        assert await policy.decide(member.user_id, "notifications", external) is Decision.CONFIRM
        await policy.set_rule(owner, "notifications", external, Decision.DENY, household=True)
        assert await policy.decide(member.user_id, "notifications", external) is Decision.DENY
        await policy.set_rule(member, "notifications", external, Decision.AUTO)
        assert await policy.decide(member.user_id, "notifications", external) is Decision.AUTO
        # Setting the same rule again replaces it.
        await policy.set_rule(member, "notifications", external, Decision.CONFIRM)
        assert await policy.decide(member.user_id, "notifications", external) is Decision.CONFIRM
        assert await policy.decide(owner.user_id, "notifications", external) is Decision.DENY
        await policy.remove_rule(member, "notifications", external)
        assert await policy.decide(member.user_id, "notifications", external) is Decision.DENY
        await policy.remove_rule(owner, "notifications", external, household=True)
        assert await policy.decide(member.user_id, "notifications", external) is Decision.CONFIRM

        rules = await policy.effective(member.user_id)
        assert len(rules) == len(DOMAINS) * len(ActionClass)
        read = next(r for r in rules if r.domain == "chat" and r.action_class is ActionClass.READ)
        assert (read.decision, read.source) == (Decision.AUTO, "default")


async def test_only_the_owner_sets_household_defaults(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    member = await principal(sessions, "boris")
    async with sessions() as session:
        policy = PolicyService(session)
        with pytest.raises(ForbiddenError):
            await policy.set_rule(member, "chat", ActionClass.READ, Decision.DENY, household=True)
        with pytest.raises(InvalidRuleError):
            await policy.set_rule(member, "weather", ActionClass.READ, Decision.DENY)


async def test_an_approval_is_decided_once(sessions: async_sessionmaker[AsyncSession]) -> None:
    anna = await principal(sessions, "anna")
    boris = await principal(sessions, "boris")
    async with sessions() as session:
        approvals = ApprovalsService(session)
        approval = await approvals.request(anna.user_id, RENAME, {"title": "Trip"})
        assert approval.status is ApprovalStatus.PENDING
        assert approval.summary == "Rename the thing to Trip"
        assert approval.tool == "mcp__chat__rename_thing"
        with pytest.raises(NotFoundError):
            await approvals.claim(boris, approval.id)
        claimed = await approvals.claim(anna, approval.id)
        assert claimed.status is ApprovalStatus.APPROVED
        assert claimed.decided_at is not None
        with pytest.raises(ApprovalClosedError, match="approved"):
            await approvals.claim(anna, approval.id)
        with pytest.raises(ApprovalClosedError):
            await approvals.reject(anna, approval.id)
        done = await approvals.finish(approval.id, ok=True, result="renamed")
        assert done is not None
        assert done.status is ApprovalStatus.EXECUTED
        assert await approvals.finish(approval.id, ok=False, result="again") is None

        rejected = await approvals.request(anna.user_id, RENAME, {"title": "No"})
        assert (await approvals.reject(anna, rejected.id)).status is ApprovalStatus.REJECTED
        pending = await approvals.history(anna, pending_only=True)
        assert pending == []
        assert len(await approvals.history(anna)) == 2
        assert await approvals.history(boris) == []


async def test_an_unanswered_request_expires(sessions: async_sessionmaker[AsyncSession]) -> None:
    anna = await principal(sessions, "anna")
    async with sessions() as session:
        approvals = ApprovalsService(session, ttl=timedelta(hours=24))
        approval = await approvals.request(anna.user_id, RENAME, {"title": "Late"})
        await session.execute(
            update(Approval)
            .where(Approval.id == approval.id)
            .values(expires_at=Approval.created_at - timedelta(seconds=1))
        )
        await session.commit()
        with pytest.raises(ApprovalClosedError, match="expired"):
            await approvals.claim(anna, approval.id)
        assert (await approvals.get(anna, approval.id)).status is ApprovalStatus.EXPIRED


async def test_undo_restores_the_before_state_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna")
    boris = await principal(sessions, "boris")
    specs = {RENAME.qualified_name: RENAME}
    STORE["thing"] = "Old"
    async with sessions() as session:
        context = ToolContext(session, anna.user_id)
        result = await RENAME.run(context, {"title": "New"})
        audit = AuditService(session)
        entry = audit.record(
            anna.user_id, RENAME, {"title": "New"}, Decision.AUTO_UNDO, result.change
        )
        await session.commit()
        assert entry.undoable
        assert (entry.before, entry.after) == ({"title": "Old"}, {"title": "New"})

        with pytest.raises(NotFoundError):
            await audit.undo(boris, entry.id, specs, context)
        undone = await audit.undo(anna, entry.id, specs, context)
        assert undone.undone_at is not None
        assert STORE["thing"] == "Old"
        with pytest.raises(NotUndoableError, match="already"):
            await audit.undo(anna, entry.id, specs, context)
        assert [e.id for e in await audit.history(anna)] == [entry.id]
        assert await audit.history(boris) == []


async def test_undo_refuses_when_the_entity_changed_since(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna")
    specs = {RENAME.qualified_name: RENAME}
    STORE["thing"] = "Old"
    async with sessions() as session:
        context = ToolContext(session, anna.user_id)
        result = await RENAME.run(context, {"title": "New"})
        audit = AuditService(session)
        entry = audit.record(
            anna.user_id, RENAME, {"title": "New"}, Decision.AUTO_UNDO, result.change
        )
        await session.commit()
        entry_id = entry.id
        STORE["thing"] = "Changed by hand"
        with pytest.raises(UndoConflictError):
            await audit.undo(anna, entry_id, specs, context)
        assert (await audit.get(anna, entry_id)).undone_at is None
        assert STORE["thing"] == "Changed by hand"

        external = audit.record(anna.user_id, RENAME, {"title": "x"}, Decision.CONFIRM, None)
        await session.commit()
        with pytest.raises(NotUndoableError, match="cannot"):
            await audit.undo(anna, external.id, specs, context)
