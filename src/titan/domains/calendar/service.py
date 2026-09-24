"""Events, occurrences in a window, busy intervals and planning preferences.

Times are stored in UTC. An event's own time zone decides the local time its
repeats keep and the days an all-day event covers (docs/spec/domains/calendar.md).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrule, rrulestr
from sqlalchemy import ColumnElement, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.accounts.models import User
from titan.domains.calendar.errors import ForbiddenError, InvalidEventError, NotFoundError
from titan.domains.calendar.models import Event, EventAttendee, EventKind, PlanningPrefs
from titan.domains.tasks import errors as tasks_errors
from titan.domains.tasks import recurrence
from titan.domains.tasks.service import TasksService

TITLE_LENGTH = 200
DESCRIPTION_LENGTH = 4000
LOCATION_LENGTH = 200
MAX_ATTENDEES = 50
MAX_DURATION = timedelta(days=31)
MAX_WINDOW = timedelta(days=92)
EVENT_FIELDS = frozenset(
    {
        "title",
        "description",
        "location",
        "starts_at",
        "ends_at",
        "all_day",
        "time_zone",
        "recurrence",
        "attendees",
        "task_id",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidEventError(f"{name!r} is not an IANA time zone") from None


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidEventError(f"{name} is a time with a time zone")
    return value.astimezone(UTC)


def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)


def all_day_bounds(
    starts_at: datetime, ends_at: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Round to the local midnights around whole days, at least one."""
    first = starts_at.astimezone(tz).date()
    local_end = ends_at.astimezone(tz)
    last = local_end.date() if local_end.time() == time(0) else local_end.date() + timedelta(days=1)
    if last <= first:
        last = first + timedelta(days=1)
    return _midnight(first, tz), _midnight(last, tz)


def _title(value: object) -> str:
    title = "" if value is None else " ".join(str(value).split())
    if not title:
        raise InvalidEventError("the title is empty")
    if len(title) > TITLE_LENGTH:
        raise InvalidEventError(f"the title is longer than {TITLE_LENGTH} characters")
    return title


def _text(value: object, limit: int, name: str) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > limit:
        raise InvalidEventError(f"the {name} is longer than {limit} characters")
    return text


def _rule(value: object) -> str | None:
    if value is None:
        return None
    try:
        return recurrence.normalize(str(value))
    except tasks_errors.InvalidRecurrenceError as exc:
        raise InvalidEventError(str(exc)) from None


@dataclass(frozen=True)
class SharedEvent:
    event: Event
    attendees: list[uuid.UUID]


@dataclass(frozen=True)
class Occurrence:
    event: Event
    starts_at: datetime
    ends_at: datetime


def occurrences_of(event: Event, start: datetime, end: datetime) -> list[Occurrence]:
    """The event's occurrences that overlap [start, end)."""
    if event.recurrence is None:
        if event.starts_at < end and event.ends_at > start:
            return [Occurrence(event, event.starts_at, event.ends_at)]
        return []
    tz = zone(event.time_zone)
    local_start = event.starts_at.astimezone(tz)
    parsed = rrulestr(event.recurrence, dtstart=local_start)
    assert isinstance(parsed, rrule)
    duration = event.ends_at - event.starts_at
    days = (event.ends_at.astimezone(tz).date() - local_start.date()).days
    found = []
    # Occurrences keep their local wall time; an all-day one spans whole local days.
    for occurs in parsed.between(start - duration, end, inc=True):
        if event.all_day:
            begins = _midnight(occurs.date(), tz)
            ends = _midnight(occurs.date() + timedelta(days=days), tz)
        else:
            begins = occurs.astimezone(UTC)
            ends = begins + duration
        if begins < end and ends > start:
            found.append(Occurrence(event, begins, ends))
    return found


def merge(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for begins, ends in sorted(intervals):
        if merged and begins <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], ends))
        else:
            merged.append((begins, ends))
    return merged


class CalendarService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------------------------------------------------- prefs

    async def prefs(self, actor: uuid.UUID) -> PlanningPrefs:
        stored = await self.session.get(PlanningPrefs, actor)
        if stored is not None:
            return stored
        return PlanningPrefs(
            user_id=actor,
            time_zone="UTC",
            work_start=time(9),
            work_end=time(17),
            work_days=[1, 2, 3, 4, 5],
            buffer_minutes=10,
            updated_at=_now(),
        )

    async def set_prefs(
        self,
        actor: uuid.UUID,
        *,
        time_zone: str,
        work_start: time,
        work_end: time,
        work_days: Iterable[int],
        buffer_minutes: int,
    ) -> PlanningPrefs:
        zone(time_zone)
        if work_end <= work_start:
            raise InvalidEventError("working hours end after they start")
        days = sorted(set(work_days))
        if not days or any(d not in range(1, 8) for d in days):
            raise InvalidEventError("working days are ISO weekdays, 1 (Monday) to 7")
        if not 0 <= buffer_minutes <= 240:
            raise InvalidEventError("the buffer is 0 to 240 minutes")
        stored = await self.session.get(PlanningPrefs, actor, with_for_update=True)
        if stored is None:
            stored = PlanningPrefs(user_id=actor)
            self.session.add(stored)
        stored.time_zone = time_zone
        stored.work_start = work_start.replace(tzinfo=None)
        stored.work_end = work_end.replace(tzinfo=None)
        stored.work_days = days
        stored.buffer_minutes = buffer_minutes
        stored.updated_at = _now()
        await self.session.commit()
        return stored

    # --------------------------------------------------------- access

    @staticmethod
    def _visible(actor: uuid.UUID) -> ColumnElement[bool]:
        attends = exists().where(EventAttendee.event_id == Event.id, EventAttendee.user_id == actor)
        return or_(Event.owner_id == actor, attends)

    async def _attendees(self, event_id: uuid.UUID) -> list[uuid.UUID]:
        rows = await self.session.scalars(
            select(EventAttendee.user_id).where(EventAttendee.event_id == event_id)
        )
        return list(rows.all())

    async def _event(self, actor: uuid.UUID, event_id: uuid.UUID, *, lock: bool = False) -> Event:
        query = select(Event).where(Event.id == event_id, self._visible(actor))
        if lock:
            query = query.with_for_update(of=Event)
        event = await self.session.scalar(query)
        if event is None:
            raise NotFoundError("event not found")
        return event

    async def _valid_attendees(self, owner: uuid.UUID, ids: Iterable[object]) -> list[uuid.UUID]:
        wanted: list[uuid.UUID] = []
        for value in ids:
            try:
                user_id = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
            except ValueError:
                raise InvalidEventError("attendees are user ids") from None
            if user_id != owner and user_id not in wanted:
                wanted.append(user_id)
        if len(wanted) > MAX_ATTENDEES:
            raise InvalidEventError(f"an event has at most {MAX_ATTENDEES} attendees")
        if wanted:
            found = set(
                (
                    await self.session.scalars(
                        select(User.id).where(User.id.in_(wanted), User.disabled_at.is_(None))
                    )
                ).all()
            )
            if len(found) != len(wanted):
                raise InvalidEventError("an attendee does not exist")
        return wanted

    async def _task(self, actor: uuid.UUID, task_id: object) -> uuid.UUID | None:
        if task_id is None:
            return None
        try:
            tid = task_id if isinstance(task_id, uuid.UUID) else uuid.UUID(str(task_id))
            return (await TasksService(self.session).get_task(actor, tid)).id
        except (ValueError, tasks_errors.NotFoundError):
            raise InvalidEventError("the linked task does not exist") from None

    @staticmethod
    def _timing(
        starts_at: datetime, ends_at: datetime, all_day: bool, tz: ZoneInfo
    ) -> tuple[datetime, datetime]:
        if all_day:
            starts_at, ends_at = all_day_bounds(starts_at, ends_at, tz)
        if ends_at <= starts_at:
            raise InvalidEventError("an event ends after it starts")
        if ends_at - starts_at > MAX_DURATION:
            raise InvalidEventError("an event lasts at most 31 days")
        return starts_at, ends_at

    # --------------------------------------------------------- events

    async def create_event(
        self,
        actor: uuid.UUID,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        *,
        all_day: bool = False,
        time_zone: str | None = None,
        description: str = "",
        location: str | None = None,
        recurrence: str | None = None,
        attendees: Iterable[uuid.UUID] = (),
        kind: EventKind = EventKind.EVENT,
        task_id: uuid.UUID | None = None,
    ) -> SharedEvent:
        tz_name = time_zone or (await self.prefs(actor)).time_zone
        begins, ends = self._timing(
            _aware(starts_at, "starts_at"), _aware(ends_at, "ends_at"), all_day, zone(tz_name)
        )
        event = Event(
            owner_id=actor,
            kind=kind,
            title=_title(title),
            description=_text(description, DESCRIPTION_LENGTH, "description"),
            location=_text(location, LOCATION_LENGTH, "location") or None,
            starts_at=begins,
            ends_at=ends,
            all_day=all_day,
            time_zone=tz_name,
            recurrence=_rule(recurrence),
            task_id=await self._task(actor, task_id),
        )
        members = await self._valid_attendees(actor, attendees)
        self.session.add(event)
        await self.session.flush()
        self.session.add_all(EventAttendee(event_id=event.id, user_id=m) for m in members)
        await self.session.commit()
        return SharedEvent(event, members)

    async def get_event(self, actor: uuid.UUID, event_id: uuid.UUID) -> SharedEvent:
        event = await self._event(actor, event_id)
        return SharedEvent(event, await self._attendees(event.id))

    async def update_event(
        self, actor: uuid.UUID, event_id: uuid.UUID, changes: Mapping[str, Any]
    ) -> SharedEvent:
        unknown = set(changes) - EVENT_FIELDS
        if unknown:
            raise InvalidEventError(f"an event has no field {sorted(unknown)[0]}")
        event = await self._event(actor, event_id, lock=True)
        if event.owner_id != actor:
            raise ForbiddenError("only the event's owner can change it")
        # Everything is checked before the event changes.
        values: dict[str, Any] = {}
        if "title" in changes:
            values["title"] = _title(changes["title"])
        if "description" in changes:
            values["description"] = _text(changes["description"], DESCRIPTION_LENGTH, "description")
        if "location" in changes:
            values["location"] = _text(changes["location"], LOCATION_LENGTH, "location") or None
        if "recurrence" in changes:
            values["recurrence"] = _rule(changes["recurrence"])
        if "task_id" in changes:
            values["task_id"] = await self._task(actor, changes["task_id"])
        tz_name = str(changes.get("time_zone") or event.time_zone)
        all_day = bool(changes.get("all_day", event.all_day))
        starts_at = _aware(changes.get("starts_at", event.starts_at), "starts_at")
        ends_at = _aware(changes.get("ends_at", event.ends_at), "ends_at")
        values["starts_at"], values["ends_at"] = self._timing(
            starts_at, ends_at, all_day, zone(tz_name)
        )
        values["time_zone"], values["all_day"] = tz_name, all_day
        members = await self._attendees(event.id)
        if "attendees" in changes:
            members = await self._valid_attendees(actor, changes["attendees"] or ())
            await self.session.execute(
                delete(EventAttendee).where(EventAttendee.event_id == event.id)
            )
            self.session.add_all(EventAttendee(event_id=event.id, user_id=m) for m in members)
        for name, value in values.items():
            setattr(event, name, value)
        event.updated_at = _now()
        await self.session.commit()
        return SharedEvent(event, members)

    async def delete_event(self, actor: uuid.UUID, event_id: uuid.UUID) -> None:
        event = await self._event(actor, event_id, lock=True)
        if event.owner_id != actor:
            raise ForbiddenError("only the event's owner can delete it")
        await self.session.execute(delete(EventAttendee).where(EventAttendee.event_id == event.id))
        await self.session.delete(event)
        await self.session.commit()

    # ---------------------------------------------------------- views

    @staticmethod
    def _window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
        start, end = _aware(start, "start"), _aware(end, "end")
        if end <= start:
            raise InvalidEventError("the window ends after it starts")
        if end - start > MAX_WINDOW:
            raise InvalidEventError("a window is at most 92 days")
        return start, end

    async def occurrences(
        self, actor: uuid.UUID, start: datetime, end: datetime
    ) -> list[Occurrence]:
        start, end = self._window(start, end)
        events = await self.session.scalars(
            select(Event).where(
                self._visible(actor),
                Event.starts_at < end,
                or_(Event.recurrence.is_not(None), Event.ends_at > start),
            )
        )
        found = [o for event in events for o in occurrences_of(event, start, end)]
        return sorted(found, key=lambda o: (o.starts_at, o.event.id))

    async def busy(
        self, actor: uuid.UUID, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        found = await self.occurrences(actor, start, end)
        return merge((max(o.starts_at, start), min(o.ends_at, end)) for o in found)
