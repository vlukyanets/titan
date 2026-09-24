"""Reminders, including the Snooze and Done actions of their notifications."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from titan.api.deps import CurrentPrincipal, Session
from titan.api.problems import PROBLEM_JSON
from titan.domains.notifications.service import NotificationsService
from titan.domains.reminders.models import LinkType, Reminder, ReminderStatus
from titan.domains.reminders.service import (
    DEFAULT_PAGE,
    DEFAULT_SNOOZE_MINUTES,
    MAX_PAGE,
    MAX_SNOOZE_MINUTES,
    TEXT_LENGTH,
    RemindersService,
)

router = APIRouter(prefix="/reminders", tags=["reminders"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}
_RRULE = "RFC 5545 RRULE without DTSTART; FREQ is DAILY, WEEKLY, MONTHLY or YEARLY, no COUNT"


def get_reminders(request: Request, session: Session) -> RemindersService:
    notifications = NotificationsService(
        session,
        pusher=request.app.state.pusher,
        push_origins=request.app.state.settings.push_allowed_origins,
    )
    return RemindersService(session, notifications=notifications)


Reminders = Annotated[RemindersService, Depends(get_reminders)]


class Link(BaseModel):
    type: LinkType
    id: uuid.UUID


class ReminderOut(BaseModel):
    id: uuid.UUID
    text: str
    fire_at: datetime = Field(description="When it fires next")
    occurs_at: datetime = Field(description="The occurrence it is about; differs when snoozed")
    recurrence: str | None = Field(description=_RRULE)
    status: ReminderStatus
    link: Link | None
    is_default: bool = Field(description="A task's default reminder, kept in step with the task")
    fired_at: datetime | None
    notification_id: uuid.UUID | None = Field(description="The notification of the last firing")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, r: Reminder) -> ReminderOut:
        link = None
        if r.link_type is not None and r.link_id is not None:
            link = Link(type=r.link_type, id=r.link_id)
        return cls(
            id=r.id,
            text=r.text,
            fire_at=r.fire_at,
            occurs_at=r.occurs_at,
            recurrence=r.recurrence,
            status=r.status,
            link=link,
            is_default=r.is_default,
            fired_at=r.fired_at,
            notification_id=r.notification_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )


class ReminderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=TEXT_LENGTH)
    fire_at: AwareDatetime
    recurrence: str | None = Field(
        default=None, max_length=200, description=_RRULE, examples=["FREQ=DAILY"]
    )
    link: Link | None = None


class ReminderPatch(BaseModel):
    """Only the fields sent change. A new fire_at or recurrence reschedules it."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=TEXT_LENGTH)
    fire_at: AwareDatetime | None = None
    recurrence: str | None = Field(
        default=None, max_length=200, description=f"{_RRULE}; `null` makes it a one-off"
    )

    @model_validator(mode="after")
    def _required_not_null(self) -> ReminderPatch:
        for name in ("text", "fire_at"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class Snooze(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minutes: int = Field(default=DEFAULT_SNOOZE_MINUTES, ge=1, le=MAX_SNOOZE_MINUTES)


@router.get(
    "",
    summary="The caller's reminders, newest first",
    description="Page with `before`: pass the id of the last reminder you have.",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def list_reminders(
    principal: CurrentPrincipal,
    reminders: Reminders,
    reminder_status: Annotated[ReminderStatus | None, Query(alias="status")] = None,
    before: Annotated[uuid.UUID | None, Query(description="Only older than this reminder")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[ReminderOut]:
    items = await reminders.reminders(
        principal.user_id, status=reminder_status, before=before, limit=limit
    )
    return [ReminderOut.of(r) for r in items]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a reminder",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def create_reminder(
    principal: CurrentPrincipal, reminders: Reminders, body: ReminderCreate
) -> ReminderOut:
    reminder = await reminders.create(
        principal.user_id,
        body.text,
        body.fire_at,
        recurrence=body.recurrence,
        link_type=body.link.type if body.link else None,
        link_id=body.link.id if body.link else None,
    )
    return ReminderOut.of(reminder)


@router.get("/{reminder_id}", summary="One reminder", responses={401: _PROBLEM, 404: _PROBLEM})
async def get_reminder(
    principal: CurrentPrincipal, reminders: Reminders, reminder_id: uuid.UUID
) -> ReminderOut:
    return ReminderOut.of(await reminders.get(principal.user_id, reminder_id))


@router.patch(
    "/{reminder_id}",
    summary="Change a reminder",
    responses={401: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def update_reminder(
    principal: CurrentPrincipal, reminders: Reminders, reminder_id: uuid.UUID, body: ReminderPatch
) -> ReminderOut:
    changes = body.model_dump(exclude_unset=True)
    return ReminderOut.of(await reminders.update(principal.user_id, reminder_id, changes))


@router.post(
    "/{reminder_id}/snooze",
    summary="Fire again later (the notification's Snooze action)",
    description="Answers the reminder that will fire: a new one-off for a recurring series.",
    responses={401: _PROBLEM, 404: _PROBLEM, 409: _PROBLEM, 422: _PROBLEM},
)
async def snooze_reminder(
    principal: CurrentPrincipal,
    reminders: Reminders,
    reminder_id: uuid.UUID,
    body: Snooze | None = None,
) -> ReminderOut:
    minutes = body.minutes if body else DEFAULT_SNOOZE_MINUTES
    return ReminderOut.of(await reminders.snooze(principal.user_id, reminder_id, minutes))


@router.post(
    "/{reminder_id}/dismiss",
    summary="Done (the notification's action); a recurring series goes on",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def dismiss_reminder(
    principal: CurrentPrincipal, reminders: Reminders, reminder_id: uuid.UUID
) -> ReminderOut:
    return ReminderOut.of(await reminders.dismiss(principal.user_id, reminder_id))


@router.delete(
    "/{reminder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a reminder, or stop a recurring series",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def delete_reminder(
    principal: CurrentPrincipal, reminders: Reminders, reminder_id: uuid.UUID
) -> Response:
    await reminders.delete(principal.user_id, reminder_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
