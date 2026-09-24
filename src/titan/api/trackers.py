"""Habit, health and finance trackers (docs/spec/domains/trackers.md)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer

from titan.api.deps import CurrentPrincipal, Session
from titan.api.problems import PROBLEM_JSON
from titan.domains.trackers.models import Direction, Entry, Period, Tracker, TrackerKind
from titan.domains.trackers.service import (
    CATEGORY_LENGTH,
    DEFAULT_PAGE,
    MAX_PAGE,
    MAX_PERIODS,
    NAME_LENGTH,
    NOTE_LENGTH,
    UNIT_LENGTH,
    Bucket,
    CategoryTotal,
    Stats,
    Target,
    TrackersService,
    target_of,
)
from titan.domains.trackers.templates import TEMPLATES, Template

router = APIRouter(prefix="/trackers", tags=["trackers"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}
_SCHEDULE = "RFC 5545 RRULE without DTSTART naming the days a habit is due"

# Stored as exact decimals with four places; sent as JSON numbers.
Value = Annotated[Decimal, PlainSerializer(float, return_type=float)]


def get_trackers(session: Session) -> TrackersService:
    return TrackersService(session)


Trackers = Annotated[TrackersService, Depends(get_trackers)]


# -------------------------------------------------------------- trackers


class TemplateOut(BaseModel):
    id: str
    kind: TrackerKind
    unit: str | None = Field(description="Empty when the caller gives it, such as a currency")
    min_value: Value | None
    max_value: Value | None

    @classmethod
    def of(cls, template: Template) -> TemplateOut:
        return cls(
            id=template.id,
            kind=template.kind,
            unit=template.unit,
            min_value=template.min_value,
            max_value=template.max_value,
        )


class TargetOut(BaseModel):
    value: Value
    period: Period
    direction: Direction

    @classmethod
    def of(cls, target: Target | None) -> TargetOut | None:
        if target is None:
            return None
        return cls(value=target.value, period=target.period, direction=target.direction)


class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float
    period: Period
    direction: Direction = Direction.AT_LEAST


class TrackerOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: TrackerKind
    unit: str
    min_value: Value | None
    max_value: Value | None
    target: TargetOut | None
    schedule: str | None = Field(description=_SCHEDULE)
    archived: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, tracker: Tracker) -> TrackerOut:
        return cls(
            id=tracker.id,
            name=tracker.name,
            kind=tracker.kind,
            unit=tracker.unit,
            min_value=tracker.min_value,
            max_value=tracker.max_value,
            target=TargetOut.of(target_of(tracker)),
            schedule=tracker.schedule,
            archived=tracker.archived,
            created_at=tracker.created_at,
            updated_at=tracker.updated_at,
        )


class TrackerCreate(BaseModel):
    """A template fills in kind, unit and bounds; fields sent override it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_LENGTH)
    template: str | None = Field(default=None, description="An id from GET /trackers/templates")
    kind: TrackerKind | None = None
    unit: str | None = Field(default=None, min_length=1, max_length=UNIT_LENGTH)
    min_value: float | None = None
    max_value: float | None = None
    target: TargetIn | None = None
    schedule: str | None = Field(
        default=None, max_length=200, description=_SCHEDULE, examples=["FREQ=WEEKLY;BYDAY=MO,WE,FR"]
    )


class TrackerPatch(BaseModel):
    """Only the fields sent change; `null` clears an optional field."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_LENGTH)
    kind: TrackerKind | None = None
    unit: str | None = Field(default=None, min_length=1, max_length=UNIT_LENGTH)
    min_value: float | None = None
    max_value: float | None = None
    target: TargetIn | None = None
    schedule: str | None = Field(default=None, max_length=200, description=_SCHEDULE)
    archived: bool | None = Field(default=None, description="An archived tracker takes no entries")


@router.get("/templates", summary="The built-in tracker templates", responses={401: _PROBLEM})
async def list_templates(principal: CurrentPrincipal) -> list[TemplateOut]:
    return [TemplateOut.of(t) for t in TEMPLATES.values()]


@router.get("", summary="The caller's trackers, by name", responses={401: _PROBLEM})
async def list_trackers(
    principal: CurrentPrincipal,
    trackers: Trackers,
    kind: TrackerKind | None = None,
    archived: Annotated[
        bool, Query(description="Archived trackers instead of active ones")
    ] = False,
) -> list[TrackerOut]:
    items = await trackers.trackers(principal.user_id, kind=kind, archived=archived)
    return [TrackerOut.of(t) for t in items]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a tracker",
    responses={401: _PROBLEM, 409: _PROBLEM, 422: _PROBLEM},
)
async def create_tracker(
    principal: CurrentPrincipal, trackers: Trackers, body: TrackerCreate
) -> TrackerOut:
    fields = body.model_dump(exclude_unset=True, exclude={"name", "template"})
    tracker = await trackers.create_tracker(
        principal.user_id, body.name, template=body.template, **fields
    )
    return TrackerOut.of(tracker)


@router.get("/{tracker_id}", summary="One tracker", responses={401: _PROBLEM, 404: _PROBLEM})
async def get_tracker(
    principal: CurrentPrincipal, trackers: Trackers, tracker_id: uuid.UUID
) -> TrackerOut:
    return TrackerOut.of(await trackers.get_tracker(principal.user_id, tracker_id))


@router.patch(
    "/{tracker_id}",
    summary="Change a tracker",
    responses={401: _PROBLEM, 404: _PROBLEM, 409: _PROBLEM, 422: _PROBLEM},
)
async def update_tracker(
    principal: CurrentPrincipal, trackers: Trackers, tracker_id: uuid.UUID, body: TrackerPatch
) -> TrackerOut:
    changes = body.model_dump(exclude_unset=True)
    return TrackerOut.of(await trackers.update_tracker(principal.user_id, tracker_id, changes))


@router.delete(
    "/{tracker_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a tracker with its entries",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def delete_tracker(
    principal: CurrentPrincipal, trackers: Trackers, tracker_id: uuid.UUID
) -> Response:
    await trackers.delete_tracker(principal.user_id, tracker_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------- entries


class EntryOut(BaseModel):
    id: uuid.UUID
    tracker_id: uuid.UUID
    at: datetime
    value: Value
    note: str
    category: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, entry: Entry) -> EntryOut:
        return cls(
            id=entry.id,
            tracker_id=entry.tracker_id,
            at=entry.at,
            value=entry.value,
            note=entry.note,
            category=entry.category,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )


class EntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float = Field(description="At most four decimal places are kept")
    at: AwareDatetime | None = Field(default=None, description="When it happened; now if empty")
    note: str = Field(default="", max_length=NOTE_LENGTH)
    category: str | None = Field(default=None, max_length=CATEGORY_LENGTH)


class EntryPatch(BaseModel):
    """Only the fields sent change; `null` clears the category."""

    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    at: AwareDatetime | None = None
    note: str | None = Field(default=None, max_length=NOTE_LENGTH)
    category: str | None = Field(default=None, max_length=CATEGORY_LENGTH)


@router.get(
    "/{tracker_id}/entries",
    summary="A tracker's entries, newest first",
    description="Page with `before`: pass the id of the last entry you have.",
    responses={401: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def list_entries(
    principal: CurrentPrincipal,
    trackers: Trackers,
    tracker_id: uuid.UUID,
    start: Annotated[AwareDatetime | None, Query(alias="from", description="Inclusive")] = None,
    end: Annotated[AwareDatetime | None, Query(alias="to", description="Exclusive")] = None,
    category: Annotated[str | None, Query(max_length=CATEGORY_LENGTH)] = None,
    before: Annotated[uuid.UUID | None, Query(description="Only older than this entry")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[EntryOut]:
    items = await trackers.entries(
        principal.user_id,
        tracker_id,
        start=start,
        end=end,
        category=category,
        before=before,
        limit=limit,
    )
    return [EntryOut.of(e) for e in items]


@router.post(
    "/{tracker_id}/entries",
    status_code=status.HTTP_201_CREATED,
    summary="Log an entry",
    responses={401: _PROBLEM, 404: _PROBLEM, 409: _PROBLEM, 422: _PROBLEM},
)
async def log_entry(
    principal: CurrentPrincipal, trackers: Trackers, tracker_id: uuid.UUID, body: EntryCreate
) -> EntryOut:
    entry = await trackers.log(
        principal.user_id,
        tracker_id,
        body.value,
        at=body.at,
        note=body.note,
        category=body.category,
    )
    return EntryOut.of(entry)


@router.patch(
    "/{tracker_id}/entries/{entry_id}",
    summary="Change an entry",
    responses={401: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def update_entry(
    principal: CurrentPrincipal,
    trackers: Trackers,
    tracker_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: EntryPatch,
) -> EntryOut:
    changes = body.model_dump(exclude_unset=True)
    entry = await trackers.update_entry(principal.user_id, tracker_id, entry_id, changes)
    return EntryOut.of(entry)


@router.delete(
    "/{tracker_id}/entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an entry",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def delete_entry(
    principal: CurrentPrincipal, trackers: Trackers, tracker_id: uuid.UUID, entry_id: uuid.UUID
) -> Response:
    await trackers.delete_entry(principal.user_id, tracker_id, entry_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------- stats


class BucketOut(BaseModel):
    start: date = Field(description="First local day of the period")
    count: int
    sum: Value
    average: Value | None
    min: Value | None
    max: Value | None
    met: bool | None = Field(
        description="Whether the period meets the target; empty when the target has another period"
    )

    @classmethod
    def of(cls, bucket: Bucket) -> BucketOut:
        return cls(
            start=bucket.start,
            count=bucket.count,
            sum=bucket.sum,
            average=bucket.average,
            min=bucket.min,
            max=bucket.max,
            met=bucket.met,
        )


class CategoryOut(BaseModel):
    category: str | None
    count: int
    sum: Value

    @classmethod
    def of(cls, total: CategoryTotal) -> CategoryOut:
        return cls(category=total.category, count=total.count, sum=total.sum)


class StatsOut(BaseModel):
    period: Period
    time_zone: str = Field(description="The owner's zone, which decides what a day is")
    start: date
    end: date = Field(description="The last day covered, inclusive")
    count: int
    sum: Value
    average: Value | None = Field(description="Per entry")
    min: Value | None
    max: Value | None
    per_period: Value = Field(description="The sum divided by the number of periods")
    buckets: list[BucketOut] = Field(description="Every period, including empty ones")
    categories: list[CategoryOut] = Field(description="Sums per category, largest first")
    streak: int = Field(description="Met periods in a row up to now")
    streak_period: Period

    @classmethod
    def of(cls, stats: Stats) -> StatsOut:
        return cls(
            period=stats.period,
            time_zone=stats.time_zone,
            start=stats.start,
            end=stats.end,
            count=stats.count,
            sum=stats.sum,
            average=stats.average,
            min=stats.min,
            max=stats.max,
            per_period=stats.per_period,
            buckets=[BucketOut.of(b) for b in stats.buckets],
            categories=[CategoryOut.of(c) for c in stats.categories],
            streak=stats.streak,
            streak_period=stats.streak_period,
        )


@router.get(
    "/{tracker_id}/stats",
    summary="Stats per local period, and the streak",
    description=f"Covers whole periods from `from` to `to`, at most {MAX_PERIODS} of them. "
    "Without `from`, the last 30 days, 12 weeks or 12 months up to `to` (today by default).",
    responses={401: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def get_stats(
    principal: CurrentPrincipal,
    trackers: Trackers,
    tracker_id: uuid.UUID,
    period: Annotated[
        Period | None, Query(description="By default the target's period, or day")
    ] = None,
    start: Annotated[date | None, Query(alias="from", description="Local date")] = None,
    end: Annotated[date | None, Query(alias="to", description="Local date, inclusive")] = None,
) -> StatsOut:
    stats = await trackers.stats(principal.user_id, tracker_id, period=period, start=start, end=end)
    return StatsOut.of(stats)
