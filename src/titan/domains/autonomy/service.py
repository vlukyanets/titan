"""Resolving policy, keeping approval requests and the audit log.

Running tools is the agent runtime's job; these services store the decisions
and their records, and own the rules for who may see or change them.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.accounts.service import Principal
from titan.domains.autonomy.errors import (
    ApprovalClosedError,
    ForbiddenError,
    InvalidRuleError,
    NotFoundError,
    NotUndoableError,
)
from titan.domains.autonomy.models import (
    DEFAULT_DECISIONS,
    DOMAINS,
    ActionClass,
    Approval,
    ApprovalStatus,
    AuditEntry,
    Decision,
    PolicyRule,
)
from titan.domains.autonomy.tools import Change, ToolContext, ToolSpec

DEFAULT_PAGE = 50
MAX_PAGE = 100
DEFAULT_APPROVAL_TTL = timedelta(hours=24)
SUMMARY_LENGTH = 500
RESULT_LENGTH = 1000


def _now() -> datetime:
    return datetime.now(UTC)


def _page(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE))


# ------------------------------------------------------------------ policy


@dataclass(frozen=True)
class EffectiveRule:
    domain: str
    action_class: ActionClass
    decision: Decision
    source: Literal["user", "household", "default"]


class PolicyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def decide(self, user_id: uuid.UUID, domain: str, action_class: ActionClass) -> Decision:
        """The user's rule, else the household default, else the built-in default."""
        rules = (
            await self.session.scalars(
                select(PolicyRule).where(
                    PolicyRule.domain == domain,
                    PolicyRule.action_class == action_class,
                    or_(PolicyRule.user_id == user_id, PolicyRule.user_id.is_(None)),
                )
            )
        ).all()
        by_scope = {rule.user_id: rule.decision for rule in rules}
        return by_scope.get(user_id) or by_scope.get(None) or DEFAULT_DECISIONS[action_class]

    async def effective(self, user_id: uuid.UUID) -> list[EffectiveRule]:
        rules = (
            await self.session.scalars(
                select(PolicyRule).where(
                    or_(PolicyRule.user_id == user_id, PolicyRule.user_id.is_(None))
                )
            )
        ).all()
        own = {(r.domain, r.action_class): r.decision for r in rules if r.user_id is not None}
        household = {(r.domain, r.action_class): r.decision for r in rules if r.user_id is None}
        result: list[EffectiveRule] = []
        for domain in DOMAINS:
            for action_class in ActionClass:
                key = (domain, action_class)
                if key in own:
                    result.append(EffectiveRule(domain, action_class, own[key], "user"))
                elif key in household:
                    result.append(EffectiveRule(domain, action_class, household[key], "household"))
                else:
                    default = DEFAULT_DECISIONS[action_class]
                    result.append(EffectiveRule(domain, action_class, default, "default"))
        return result

    async def set_rule(
        self,
        actor: Principal,
        domain: str,
        action_class: ActionClass,
        decision: Decision,
        *,
        household: bool = False,
    ) -> None:
        user_id = self._scope(actor, domain, household)
        statement = insert(PolicyRule).values(
            user_id=user_id,
            domain=domain,
            action_class=action_class,
            decision=decision,
            updated_at=_now(),
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_policy_rules_user_id",
                set_={"decision": statement.excluded.decision, "updated_at": _now()},
            )
        )
        await self.session.commit()

    async def remove_rule(
        self, actor: Principal, domain: str, action_class: ActionClass, *, household: bool = False
    ) -> None:
        user_id = self._scope(actor, domain, household)
        scope = PolicyRule.user_id.is_(None) if user_id is None else PolicyRule.user_id == user_id
        await self.session.execute(
            delete(PolicyRule).where(
                scope, PolicyRule.domain == domain, PolicyRule.action_class == action_class
            )
        )
        await self.session.commit()

    @staticmethod
    def _scope(actor: Principal, domain: str, household: bool) -> uuid.UUID | None:
        if domain not in DOMAINS:
            raise InvalidRuleError(f"unknown domain {domain!r}")
        if household:
            if not actor.is_owner:
                raise ForbiddenError("only the owner sets household defaults")
            return None
        return actor.user_id


# --------------------------------------------------------------- approvals


class ApprovalsService:
    def __init__(self, session: AsyncSession, *, ttl: timedelta = DEFAULT_APPROVAL_TTL) -> None:
        self.session = session
        self.ttl = ttl

    async def request(
        self,
        user_id: uuid.UUID,
        spec: ToolSpec,
        tool_input: dict[str, Any],
        *,
        thread_id: uuid.UUID | None = None,
    ) -> Approval:
        approval = Approval(
            user_id=user_id,
            thread_id=thread_id,
            tool=spec.qualified_name,
            domain=spec.domain,
            action_class=spec.action_class,
            input=tool_input,
            summary=spec.summarize(tool_input)[:SUMMARY_LENGTH],
            status=ApprovalStatus.PENDING,
            expires_at=_now() + self.ttl,
        )
        self.session.add(approval)
        await self.session.commit()
        return approval

    async def history(
        self,
        actor: Principal,
        *,
        pending_only: bool = False,
        before: uuid.UUID | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[Approval]:
        await self._expire(actor.user_id)
        query = select(Approval).where(Approval.user_id == actor.user_id)
        if pending_only:
            query = query.where(Approval.status == ApprovalStatus.PENDING)
        if before is not None:
            query = query.where(Approval.id < before)
        query = query.order_by(Approval.id.desc()).limit(_page(limit))
        return list((await self.session.scalars(query)).all())

    async def get(self, actor: Principal, approval_id: uuid.UUID) -> Approval:
        await self._expire(actor.user_id)
        approval = await self.session.get(Approval, approval_id, populate_existing=True)
        # Someone else's request is reported as missing, so ids cannot be probed.
        if approval is None or approval.user_id != actor.user_id:
            raise NotFoundError("approval request not found")
        return approval

    async def claim(self, actor: Principal, approval_id: uuid.UUID) -> Approval:
        """Take a pending request for running it. Only one caller can win."""
        return await self._decide(actor, approval_id, ApprovalStatus.APPROVED)

    async def reject(self, actor: Principal, approval_id: uuid.UUID) -> Approval:
        return await self._decide(actor, approval_id, ApprovalStatus.REJECTED)

    async def finish(
        self,
        approval_id: uuid.UUID,
        *,
        ok: bool,
        result: str,
        audit_entry_id: uuid.UUID | None = None,
    ) -> Approval | None:
        approval = await self.session.scalar(
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == ApprovalStatus.APPROVED)
            .values(
                status=ApprovalStatus.EXECUTED if ok else ApprovalStatus.FAILED,
                result=result[:RESULT_LENGTH],
                audit_entry_id=audit_entry_id,
            )
            .returning(Approval)
            .execution_options(populate_existing=True)
        )
        await self.session.commit()
        return approval

    async def _decide(
        self, actor: Principal, approval_id: uuid.UUID, status: ApprovalStatus
    ) -> Approval:
        approval = await self.get(actor, approval_id)
        decided = await self.session.scalar(
            update(Approval)
            .where(
                Approval.id == approval.id,
                Approval.status == ApprovalStatus.PENDING,
                Approval.expires_at > _now(),
            )
            .values(status=status, decided_at=_now())
            .returning(Approval)
            .execution_options(populate_existing=True)
        )
        await self.session.commit()
        if decided is None:
            current = await self.get(actor, approval_id)
            raise ApprovalClosedError(f"the request is already {current.status.value}")
        return decided

    async def _expire(self, user_id: uuid.UUID) -> None:
        result = await self.session.execute(
            update(Approval)
            .where(
                Approval.user_id == user_id,
                Approval.status == ApprovalStatus.PENDING,
                Approval.expires_at <= _now(),
            )
            .values(status=ApprovalStatus.EXPIRED, decided_at=_now())
        )
        if getattr(result, "rowcount", 0):
            await self.session.commit()


# --------------------------------------------------------------- audit log


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def record(
        self,
        user_id: uuid.UUID,
        spec: ToolSpec,
        tool_input: dict[str, Any],
        decision: Decision,
        change: Change | None,
        *,
        approval_id: uuid.UUID | None = None,
    ) -> AuditEntry:
        """Add an entry to the session; the caller commits it with the change."""
        entry = AuditEntry(
            user_id=user_id,
            approval_id=approval_id,
            tool=spec.qualified_name,
            domain=spec.domain,
            action_class=spec.action_class,
            decision=decision,
            summary=spec.summarize(tool_input)[:SUMMARY_LENGTH],
            input=tool_input,
            entity_type=change.entity_type if change else None,
            entity_id=change.entity_id if change else None,
            before=change.before if change else None,
            after=change.after if change else None,
            undoable=change is not None and spec.undo is not None,
        )
        self.session.add(entry)
        return entry

    async def history(
        self, actor: Principal, *, before: uuid.UUID | None = None, limit: int = DEFAULT_PAGE
    ) -> list[AuditEntry]:
        query = select(AuditEntry).where(AuditEntry.user_id == actor.user_id)
        if before is not None:
            query = query.where(AuditEntry.id < before)
        query = query.order_by(AuditEntry.id.desc()).limit(_page(limit))
        return list((await self.session.scalars(query)).all())

    async def get(self, actor: Principal, entry_id: uuid.UUID) -> AuditEntry:
        entry = await self.session.get(AuditEntry, entry_id, populate_existing=True)
        if entry is None or entry.user_id != actor.user_id:
            raise NotFoundError("audit entry not found")
        return entry

    async def undo(
        self,
        actor: Principal,
        entry_id: uuid.UUID,
        specs: Mapping[str, ToolSpec],
        context: ToolContext,
    ) -> AuditEntry:
        """Restore the entry's before state if the entity still has its after state.

        `context.session` must be this service's session, so the undo and the
        entry's `undone_at` commit together.
        """
        # Lock the entry so two undos of it cannot both run.
        entry = await self.session.scalar(
            select(AuditEntry)
            .where(AuditEntry.id == entry_id, AuditEntry.user_id == actor.user_id)
            .with_for_update()
        )
        if entry is None:
            raise NotFoundError("audit entry not found")
        spec = specs.get(entry.tool)
        if entry.undone_at is not None:
            raise NotUndoableError("this action was already undone")
        if not entry.undoable or spec is None or spec.undo is None:
            raise NotUndoableError("this action cannot be undone")
        if entry.entity_type is None or entry.entity_id is None or entry.before is None:
            raise NotUndoableError("this action cannot be undone")
        change = Change(entry.entity_type, entry.entity_id, entry.before, entry.after or {})
        try:
            await spec.undo(context, change)
        except BaseException:
            await self.session.rollback()
            raise
        entry.undone_at = _now()
        await self.session.commit()
        return entry
