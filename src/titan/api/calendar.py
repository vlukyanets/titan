"""Calendar events, day and week views, busy intervals and planning preferences."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from titan.api.deps import CurrentPrincipal, Session
from titan.api.problems import PROBLEM_JSON
from titan.domains.calendar.models import Event, EventKind, PlanningPrefs
from titan.domains.calendar.service import (
    DESCRIPTION_LENGTH,
    LOCATION_LENGTH,
    MAX_ATTENDEES,
    TITLE_LENGTH,
    CalendarService,
    Occurrence,
    SharedEvent,
)

router = APIRouter(prefix="/calendar", tags=["calendar"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}
_RRULE = "RFC 5545 RRULE without DTSTART, repeated in the event's time zone; no COUNT"
_ZONE = "IANA time zone, such as Europe/Kyiv"


def get_calendar(session: Session) -> CalendarService:
    return CalendarService(session)


Calendar = Annotated[CalendarService, Depends(get_calendar)]

WindowStart = Annotated[AwareDatetime, Query(description="Start of the window")]
WindowEnd = Annotated[AwareDatetime, Query(description="End of the window; at most 92 days later")]


class EventOut(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    kind: EventKind
    title: str
    description: str
    location: str | None
    starts_at: datetime = Field(description="Start of the event, or of its first occurrence")
    ends_at: datetime
    all_day: bool = Field(description="Whole local days: starts and ends at local midnight")
    time_zone: str = Field(description=_ZONE)
    recurrence: str | None = Field(description=_RRULE)
    attendees: list[uuid.UUID]
    task_id: uuid.UUID | None = Field(description="The task of a time block")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, shared: SharedEvent) -> EventOut:
        e: Event = shared.event
        return cls(
            id=e.id,
            owner_id=e.owner_id,
            kind=e.kind,
            title=e.title,
            description=e.description,
            location=e.location,
            starts_at=e.starts_at,
            ends_at=e.ends_at,
            all_day=e.all_day,
            time_zone=e.time_zone,
            recurrence=e.recurrence,
            attendees=shared.attendees,
            task_id=e.task_id,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


class OccurrenceOut(BaseModel):
    event_id: uuid.UUID
    owner_id: uuid.UUID
    kind: EventKind
    title: str
    location: str | None
    starts_at: datetime = Field(description="This occurrence's start")
    ends_at: datetime
    all_day: bool
    time_zone: str
    recurring: bool
    task_id: uuid.UUID | None

    @classmethod
    def of(cls, o: Occurrence) -> OccurrenceOut:
        return cls(
            event_id=o.event.id,
            owner_id=o.event.owner_id,
            kind=o.event.kind,
            title=o.event.title,
            location=o.event.location,
            starts_at=o.starts_at,
            ends_at=o.ends_at,
            all_day=o.event.all_day,
            time_zone=o.event.time_zone,
            recurring=o.event.recurrence is not None,
            task_id=o.event.task_id,
        )


class Interval(BaseModel):
    starts_at: datetime
    ends_at: datetime


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=TITLE_LENGTH)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    all_day: bool = False
    time_zone: str | None = Field(
        default=None, max_length=64, description=f"{_ZONE}; the caller's own by default"
    )
    description: str = Field(default="", max_length=DESCRIPTION_LENGTH)
    location: str | None = Field(default=None, max_length=LOCATION_LENGTH)
    recurrence: str | None = Field(
        default=None, max_length=200, description=_RRULE, examples=["FREQ=WEEKLY;BYDAY=MO"]
    )
    attendees: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_ATTENDEES)
    kind: EventKind = EventKind.EVENT
    task_id: uuid.UUID | None = None


class EventPatch(BaseModel):
    """Only the fields sent change; `null` clears an optional field."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_LENGTH)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    all_day: bool | None = None
    time_zone: str | None = Field(default=None, max_length=64, description=_ZONE)
    description: str | None = Field(default=None, max_length=DESCRIPTION_LENGTH)
    location: str | None = Field(default=None, max_length=LOCATION_LENGTH)
    recurrence: str | None = Field(default=None, max_length=200, description=_RRULE)
    attendees: list[uuid.UUID] | None = Field(
        default=None, max_length=MAX_ATTENDEES, description="Replaces the whole list"
    )
    task_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _required_not_null(self) -> EventPatch:
        for name in ("title", "starts_at", "ends_at", "all_day", "time_zone"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class PrefsIO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_zone: str = Field(max_length=64, description=_ZONE, examples=["Europe/Kyiv"])
    work_start: time = Field(description="Local start of working hours")
    work_end: time
    work_days: list[int] = Field(
        min_length=1, max_length=7, description="ISO weekdays, 1 is Monday"
    )
    buffer_minutes: int = Field(ge=0, le=240, description="Gap the planner keeps between blocks")
    default_reminder_minutes: int | None = Field(
        default=15,
        ge=1,
        le=1440,
        description="How long before its due time a task reminds its owner; null turns it off",
    )

    @classmethod
    def of(cls, prefs: PlanningPrefs) -> PrefsIO:
        return cls(
            time_zone=prefs.time_zone,
            work_start=prefs.work_start,
            work_end=prefs.work_end,
            work_days=list(prefs.work_days),
            buffer_minutes=prefs.buffer_minutes,
            default_reminder_minutes=prefs.default_reminder_minutes,
        )


@router.get(
    "/events",
    summary="Occurrences in a window, own and attended, by start",
    description="Recurring events are expanded in their own time zone.",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def list_occurrences(
    principal: CurrentPrincipal, calendar: Calendar, start: WindowStart, end: WindowEnd
) -> list[OccurrenceOut]:
    return [OccurrenceOut.of(o) for o in await calendar.occurrences(principal.user_id, start, end)]


@router.post(
    "/events",
    status_code=status.HTTP_201_CREATED,
    summary="Create an event",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def create_event(
    principal: CurrentPrincipal, calendar: Calendar, body: EventCreate
) -> EventOut:
    return EventOut.of(await calendar.create_event(principal.user_id, **body.model_dump()))


@router.get("/events/{event_id}", summary="One event", responses={401: _PROBLEM, 404: _PROBLEM})
async def get_event(
    principal: CurrentPrincipal, calendar: Calendar, event_id: uuid.UUID
) -> EventOut:
    return EventOut.of(await calendar.get_event(principal.user_id, event_id))


@router.patch(
    "/events/{event_id}",
    summary="Change an event (owner only)",
    responses={401: _PROBLEM, 403: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def update_event(
    principal: CurrentPrincipal, calendar: Calendar, event_id: uuid.UUID, body: EventPatch
) -> EventOut:
    changes = body.model_dump(exclude_unset=True)
    return EventOut.of(await calendar.update_event(principal.user_id, event_id, changes))


@router.delete(
    "/events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an event (owner only)",
    responses={401: _PROBLEM, 403: _PROBLEM, 404: _PROBLEM},
)
async def delete_event(
    principal: CurrentPrincipal, calendar: Calendar, event_id: uuid.UUID
) -> Response:
    await calendar.delete_event(principal.user_id, event_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/busy",
    summary="The caller's merged busy intervals in a window",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def busy(
    principal: CurrentPrincipal, calendar: Calendar, start: WindowStart, end: WindowEnd
) -> list[Interval]:
    intervals = await calendar.busy(principal.user_id, start, end)
    return [Interval(starts_at=b, ends_at=e) for b, e in intervals]


@router.get("/prefs", summary="The caller's planning preferences", responses={401: _PROBLEM})
async def get_prefs(principal: CurrentPrincipal, calendar: Calendar) -> PrefsIO:
    return PrefsIO.of(await calendar.prefs(principal.user_id))


@router.put(
    "/prefs",
    summary="Set the caller's planning preferences",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def put_prefs(principal: CurrentPrincipal, calendar: Calendar, body: PrefsIO) -> PrefsIO:
    return PrefsIO.of(await calendar.set_prefs(principal.user_id, **body.model_dump()))
