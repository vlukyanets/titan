"""Token usage and cost per user and month."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from titan.api.deps import CurrentPrincipal, Session
from titan.api.problems import PROBLEM_JSON
from titan.domains.usage.service import MonthUsage, Totals, UsageService, current_month

router = APIRouter(prefix="/usage", tags=["usage"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}

Month = Annotated[
    str | None,
    Query(
        description="Calendar month in UTC, YYYY-MM; the current month by default",
        examples=["2026-09"],
    ),
]


class TotalsOut(BaseModel):
    sessions: int = Field(description="Agent sessions that reported usage")
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float = Field(
        description=(
            "As Claude Code reports it; in oauth mode, what the tokens would cost on the API"
        )
    )

    @classmethod
    def of(cls, totals: Totals) -> TotalsOut:
        return cls(
            sessions=totals.sessions,
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            cache_read_tokens=totals.cache_read_tokens,
            cache_creation_tokens=totals.cache_creation_tokens,
            cost_usd=round(totals.cost_usd, 6),
        )


class ModelTotalsOut(TotalsOut):
    model: str


class MonthUsageOut(BaseModel):
    month: str
    total: TotalsOut
    by_model: list[ModelTotalsOut]

    @classmethod
    def of(cls, usage: MonthUsage) -> MonthUsageOut:
        return cls(
            month=usage.month,
            total=TotalsOut.of(usage.total),
            by_model=[
                ModelTotalsOut(model=model, **TotalsOut.of(totals).model_dump())
                for model, totals in sorted(usage.by_model.items())
            ],
        )


class MemberUsageOut(MonthUsageOut):
    user_id: uuid.UUID
    username: str


def get_usage(session: Session) -> UsageService:
    return UsageService(session)


Usage = Annotated[UsageService, Depends(get_usage)]


@router.get("", summary="The caller's usage for a month", responses={401: _PROBLEM, 422: _PROBLEM})
async def my_usage(principal: CurrentPrincipal, usage: Usage, month: Month = None) -> MonthUsageOut:
    return MonthUsageOut.of(await usage.month(principal, month or current_month()))


@router.get(
    "/household",
    summary="Every user's usage for a month (owner only)",
    responses={401: _PROBLEM, 403: _PROBLEM, 422: _PROBLEM},
)
async def household_usage(
    principal: CurrentPrincipal, usage: Usage, month: Month = None
) -> list[MemberUsageOut]:
    members = await usage.household(principal, month or current_month())
    return [
        MemberUsageOut(
            user_id=m.user_id, username=m.username, **MonthUsageOut.of(m.usage).model_dump()
        )
        for m in members
    ]
