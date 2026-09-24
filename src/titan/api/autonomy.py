"""Autonomy policy, approval requests and the audit log."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from titan.agent.tools import REGISTRY, ToolScope, run_approved
from titan.api.deps import CurrentPrincipal, Session
from titan.api.problems import PROBLEM_JSON
from titan.domains.autonomy.models import (
    ActionClass,
    Approval,
    ApprovalStatus,
    AuditEntry,
    Decision,
)
from titan.domains.autonomy.service import (
    DEFAULT_PAGE,
    MAX_PAGE,
    ApprovalsService,
    AuditService,
    PolicyService,
)

router = APIRouter(tags=["autonomy"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}


def _scope(request: Request, user_id: uuid.UUID, thread_id: uuid.UUID | None) -> ToolScope:
    settings = request.app.state.settings
    return ToolScope(
        request.app.state.sessions,
        user_id,
        thread_id,
        request.app.state.pusher,
        settings.push_allowed_origins,
    )


# ------------------------------------------------------------------ policy


class PolicyRuleOut(BaseModel):
    domain: str
    action_class: ActionClass
    decision: Decision
    source: Literal["user", "household", "default"] = Field(
        description="Your own rule, the owner's household default, or the built-in default"
    )


class PolicyRuleIn(BaseModel):
    decision: Decision


def get_policy(session: Session) -> PolicyService:
    return PolicyService(session)


Policy = Annotated[PolicyService, Depends(get_policy)]


@router.get(
    "/policy",
    summary="The caller's effective policy for every domain and action class",
    responses={401: _PROBLEM},
)
async def effective_policy(principal: CurrentPrincipal, policy: Policy) -> list[PolicyRuleOut]:
    rules = await policy.effective(principal.user_id)
    return [
        PolicyRuleOut(
            domain=r.domain, action_class=r.action_class, decision=r.decision, source=r.source
        )
        for r in rules
    ]


@router.put(
    "/policy/{domain}/{action_class}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set the caller's own rule",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def set_rule(
    domain: str,
    action_class: ActionClass,
    body: PolicyRuleIn,
    principal: CurrentPrincipal,
    policy: Policy,
) -> Response:
    await policy.set_rule(principal, domain, action_class, body.decision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/policy/{domain}/{action_class}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove the caller's own rule",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def remove_rule(
    domain: str, action_class: ActionClass, principal: CurrentPrincipal, policy: Policy
) -> Response:
    await policy.remove_rule(principal, domain, action_class)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/policy/household/{domain}/{action_class}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a household default (owner only)",
    responses={401: _PROBLEM, 403: _PROBLEM, 422: _PROBLEM},
)
async def set_household_rule(
    domain: str,
    action_class: ActionClass,
    body: PolicyRuleIn,
    principal: CurrentPrincipal,
    policy: Policy,
) -> Response:
    await policy.set_rule(principal, domain, action_class, body.decision, household=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/policy/household/{domain}/{action_class}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a household default (owner only)",
    responses={401: _PROBLEM, 403: _PROBLEM, 422: _PROBLEM},
)
async def remove_household_rule(
    domain: str, action_class: ActionClass, principal: CurrentPrincipal, policy: Policy
) -> Response:
    await policy.remove_rule(principal, domain, action_class, household=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------- approvals


class ApprovalOut(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID | None = Field(description="The chat thread the request came from")
    tool: str
    domain: str
    action_class: ActionClass
    input: dict[str, Any] = Field(description="Exactly what runs on approval")
    summary: str = Field(description="What is being asked, for people")
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    result: str | None = Field(description="What happened when it ran")

    @classmethod
    def of(cls, approval: Approval) -> ApprovalOut:
        return cls(
            id=approval.id,
            thread_id=approval.thread_id,
            tool=approval.tool,
            domain=approval.domain,
            action_class=approval.action_class,
            input=approval.input,
            summary=approval.summary,
            status=approval.status,
            created_at=approval.created_at,
            expires_at=approval.expires_at,
            decided_at=approval.decided_at,
            result=approval.result,
        )


def get_approvals(request: Request, session: Session) -> ApprovalsService:
    hours = request.app.state.settings.approval_ttl_hours
    return ApprovalsService(session, ttl=timedelta(hours=hours))


Approvals = Annotated[ApprovalsService, Depends(get_approvals)]


@router.get(
    "/approvals",
    summary="The caller's approval requests, newest first",
    description="Page with `before`: pass the id of the last request you have.",
    responses={401: _PROBLEM},
)
async def list_approvals(
    principal: CurrentPrincipal,
    approvals: Approvals,
    pending: Annotated[bool, Query(description="Only requests still waiting")] = False,
    before: Annotated[uuid.UUID | None, Query(description="Only older than this id")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[ApprovalOut]:
    items = await approvals.history(principal, pending_only=pending, before=before, limit=limit)
    return [ApprovalOut.of(a) for a in items]


@router.get(
    "/approvals/{approval_id}",
    summary="One approval request",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def get_approval(
    approval_id: uuid.UUID, principal: CurrentPrincipal, approvals: Approvals
) -> ApprovalOut:
    return ApprovalOut.of(await approvals.get(principal, approval_id))


@router.post(
    "/approvals/{approval_id}/approve",
    summary="Approve a request and run it",
    description=(
        "Runs exactly the stored call and answers with the request, now `executed` or "
        "`failed`, and its result. The result is also posted to the chat thread."
    ),
    responses={401: _PROBLEM, 404: _PROBLEM, 409: _PROBLEM},
)
async def approve(
    approval_id: uuid.UUID,
    request: Request,
    principal: CurrentPrincipal,
    approvals: Approvals,
) -> ApprovalOut:
    claimed = await approvals.claim(principal, approval_id)
    scope = _scope(request, principal.user_id, claimed.thread_id)
    return ApprovalOut.of(await run_approved(claimed, scope))


@router.post(
    "/approvals/{approval_id}/reject",
    summary="Reject a request",
    responses={401: _PROBLEM, 404: _PROBLEM, 409: _PROBLEM},
)
async def reject(
    approval_id: uuid.UUID, principal: CurrentPrincipal, approvals: Approvals
) -> ApprovalOut:
    return ApprovalOut.of(await approvals.reject(principal, approval_id))


# --------------------------------------------------------------- audit log


class AuditEntryOut(BaseModel):
    id: uuid.UUID
    approval_id: uuid.UUID | None
    tool: str
    domain: str
    action_class: ActionClass
    decision: Decision = Field(description="What allowed it; `confirm` means it was approved")
    summary: str
    input: dict[str, Any]
    entity_type: str | None
    entity_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    undoable: bool = Field(description="Whether undo is still possible")
    created_at: datetime
    undone_at: datetime | None

    @classmethod
    def of(cls, entry: AuditEntry) -> AuditEntryOut:
        return cls(
            id=entry.id,
            approval_id=entry.approval_id,
            tool=entry.tool,
            domain=entry.domain,
            action_class=entry.action_class,
            decision=entry.decision,
            summary=entry.summary,
            input=entry.input,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            before=entry.before,
            after=entry.after,
            undoable=entry.undoable and entry.undone_at is None,
            created_at=entry.created_at,
            undone_at=entry.undone_at,
        )


def get_audit(session: Session) -> AuditService:
    return AuditService(session)


Audit = Annotated[AuditService, Depends(get_audit)]


@router.get(
    "/audit",
    summary="The caller's audit log, newest first",
    description="Page with `before`: pass the id of the last entry you have.",
    responses={401: _PROBLEM},
)
async def list_audit(
    principal: CurrentPrincipal,
    audit: Audit,
    before: Annotated[uuid.UUID | None, Query(description="Only older than this id")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[AuditEntryOut]:
    return [AuditEntryOut.of(e) for e in await audit.history(principal, before=before, limit=limit)]


@router.post(
    "/audit/{entry_id}/undo",
    summary="Undo one logged action",
    description="Answers `409` if it cannot be undone, or if the entity changed since.",
    responses={401: _PROBLEM, 404: _PROBLEM, 409: _PROBLEM},
)
async def undo(
    entry_id: uuid.UUID,
    request: Request,
    session: Session,
    principal: CurrentPrincipal,
    audit: Audit,
) -> AuditEntryOut:
    scope = _scope(request, principal.user_id, None)
    entry = await audit.undo(principal, entry_id, REGISTRY, scope.context(session))
    return AuditEntryOut.of(entry)
