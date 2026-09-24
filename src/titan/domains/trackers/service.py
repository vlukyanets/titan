"""Trackers, entries, stats and streaks, with the rules of docs/spec/domains/trackers.md.

Every tracker belongs to one user. Someone else's tracker is reported as
missing, so ids cannot be probed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.calendar.zones import user_zone
from titan.domains.tasks import recurrence
from titan.domains.tasks.errors import InvalidRecurrenceError
from titan.domains.trackers.errors import (
    ArchivedError,
    DuplicateNameError,
    InvalidEntryError,
    InvalidTrackerError,
    NotFoundError,
)
from titan.domains.trackers.models import Direction, Entry, Period, Tracker, TrackerKind
from titan.domains.trackers.templates import TEMPLATES

DEFAULT_PAGE = 50
MAX_PAGE = 200
NAME_LENGTH = 100
UNIT_LENGTH = 16
NOTE_LENGTH = 1000
CATEGORY_LENGTH = 32
MAX_PERIODS = 400
MAX_STREAK = 1000
# Periods covered when a stats request gives no start.
DEFAULT_PERIODS = {Period.DAY: 30, Period.WEEK: 12, Period.MONTH: 12}
# Numeric(18, 4): 14 digits before the point.
VALUE_LIMIT = Decimal(10) ** 14
_PLACES = Decimal("0.0001")

TRACKER_FIELDS = frozenset(
    {"name", "kind", "unit", "min_value", "max_value", "target", "schedule", "archived"}
)
ENTRY_FIELDS = frozenset({"at", "value", "note", "category"})


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Target:
    value: Decimal
    period: Period
    direction: Direction


@dataclass(frozen=True)
class Bucket:
    start: date
    count: int
    sum: Decimal
    average: Decimal | None
    min: Decimal | None
    max: Decimal | None
    # None when the tracker has no target.
    met: bool | None


@dataclass(frozen=True)
class CategoryTotal:
    category: str | None
    count: int
    sum: Decimal


@dataclass(frozen=True)
class Stats:
    period: Period
    time_zone: str
    start: date
    # The last day covered, inclusive.
    end: date
    count: int
    sum: Decimal
    average: Decimal | None
    min: Decimal | None
    max: Decimal | None
    per_period: Decimal
    buckets: list[Bucket]
    categories: list[CategoryTotal]
    streak: int
    streak_period: Period


# ------------------------------------------------------------ validation


def _value(value: object, name: str, error: type[Exception] = InvalidEntryError) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        raise error(f"{name} is not a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise error(f"{name} is not a number") from None
    if not number.is_finite():
        raise error(f"{name} is not a number")
    number = number.quantize(_PLACES, rounding=ROUND_HALF_EVEN)
    if abs(number) >= VALUE_LIMIT:
        raise error(f"{name} is too large")
    return number


def _bound(value: object, name: str) -> Decimal | None:
    return None if value is None else _value(value, name, InvalidTrackerError)


def _name(value: object) -> str:
    name = "" if value is None else " ".join(str(value).split())
    if not name:
        raise InvalidTrackerError("the name is empty")
    if len(name) > NAME_LENGTH:
        raise InvalidTrackerError(f"the name is longer than {NAME_LENGTH} characters")
    return name


def _unit(value: object) -> str:
    unit = "" if value is None else str(value).strip()
    if not unit or len(unit) > UNIT_LENGTH:
        raise InvalidTrackerError(f"the unit is 1 to {UNIT_LENGTH} characters")
    return unit


def _kind(value: object) -> TrackerKind:
    try:
        return TrackerKind(str(value))
    except ValueError:
        raise InvalidTrackerError("the kind is habit, health, finance or custom") from None


def _target(value: object) -> Target | None:
    if value is None or isinstance(value, Target):
        return value
    if not isinstance(value, Mapping):
        raise InvalidTrackerError("the target has a value, a period and a direction")
    try:
        period = Period(str(value.get("period")))
    except ValueError:
        raise InvalidTrackerError("the target's period is day, week or month") from None
    try:
        direction = Direction(str(value.get("direction", Direction.AT_LEAST)))
    except ValueError:
        raise InvalidTrackerError("the target's direction is at_least or at_most") from None
    return Target(_value(value.get("value"), "the target", InvalidTrackerError), period, direction)


def _schedule(value: object) -> str | None:
    if value is None:
        return None
    try:
        return recurrence.normalize(str(value))
    except InvalidRecurrenceError as exc:
        raise InvalidTrackerError(f"schedule: {exc}") from None


def _at(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidEntryError("at needs a time zone")
    return value.astimezone(UTC)


def _note(value: object) -> str:
    note = "" if value is None else str(value).strip()
    if len(note) > NOTE_LENGTH:
        raise InvalidEntryError(f"the note is longer than {NOTE_LENGTH} characters")
    return note


def _category(value: object) -> str | None:
    category = "" if value is None else str(value).strip().lower()
    if not category:
        return None
    if len(category) > CATEGORY_LENGTH:
        raise InvalidEntryError(f"a category is at most {CATEGORY_LENGTH} characters")
    return category


def _check_tracker(values: Mapping[str, Any]) -> None:
    """The rules that span fields, on the tracker as it will be stored."""
    low, high = values["min_value"], values["max_value"]
    if low is not None and high is not None and low > high:
        raise InvalidTrackerError("min_value is above max_value")
    if values["schedule"] is not None and values["target_period"] not in (None, Period.DAY):
        raise InvalidTrackerError("a schedule needs a daily target or no target")


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _check_value(tracker: Tracker, value: Decimal) -> None:
    if tracker.min_value is not None and value < tracker.min_value:
        raise InvalidEntryError(f"the value is below {_plain(tracker.min_value)}")
    if tracker.max_value is not None and value > tracker.max_value:
        raise InvalidEntryError(f"the value is above {_plain(tracker.max_value)}")


def _target_columns(target: Target | None) -> dict[str, Any]:
    return {
        "target_value": target.value if target else None,
        "target_period": target.period if target else None,
        "target_direction": target.direction if target else None,
    }


def target_of(tracker: Tracker) -> Target | None:
    if tracker.target_value is None or tracker.target_period is None:
        return None
    return Target(
        tracker.target_value, tracker.target_period, tracker.target_direction or Direction.AT_LEAST
    )


# --------------------------------------------------------------- periods


def period_start(day: date, period: Period) -> date:
    if period is Period.WEEK:
        return day - timedelta(days=day.weekday())
    if period is Period.MONTH:
        return day.replace(day=1)
    return day


def next_period(start: date, period: Period) -> date:
    if period is Period.WEEK:
        return start + timedelta(days=7)
    if period is Period.MONTH:
        return date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start + timedelta(days=1)


def shift(start: date, period: Period, count: int) -> date:
    """The period start `count` periods before `start`, a period start itself."""
    if period is Period.WEEK:
        return start - timedelta(days=7 * count)
    if period is Period.MONTH:
        months = start.year * 12 + start.month - 1 - count
        return date(months // 12, months % 12 + 1, 1)
    return start - timedelta(days=count)


def periods(first: date, last: date, period: Period) -> list[date]:
    """Starts of the periods from the one holding `first` to the one holding `last`."""
    starts: list[date] = []
    current = period_start(first, period)
    while current <= last:
        starts.append(current)
        current = next_period(current, period)
    return starts


def _midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(), tz).astimezone(UTC)


def is_met(total: Decimal, count: int, target: Target | None) -> bool:
    if target is None:
        return count > 0
    if target.direction is Direction.AT_MOST:
        return total <= target.value
    return total >= target.value


def _round(value: Decimal) -> Decimal:
    return value.quantize(_PLACES, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class _Row:
    count: int
    sum: Decimal
    average: Decimal | None
    min: Decimal | None
    max: Decimal | None


class TrackersService:
    def __init__(self, session: AsyncSession, *, commit: bool = True) -> None:
        self.session = session
        # Agent tools pass commit=False: their caller commits the change together
        # with its audit entry.
        self._commit = commit

    async def _done(self) -> None:
        if self._commit:
            await self.session.commit()
        else:
            await self.session.flush()

    # -------------------------------------------------------------- trackers

    async def _unique(
        self, actor: uuid.UUID, name: str, *, besides: uuid.UUID | None = None
    ) -> None:
        # Checked here rather than by an index: a clash across replicas is
        # resolved by the owner anyway. Compared in Python, because lower() in
        # the database follows its locale and leaves Cyrillic alone under C.
        rows = await self.session.execute(
            select(Tracker.id, Tracker.name).where(Tracker.owner_id == actor)
        )
        wanted = name.casefold()
        if any(i != besides and other.casefold() == wanted for i, other in rows):
            raise DuplicateNameError(f"there is already a tracker named {name!r}")

    async def create_tracker(
        self, actor: uuid.UUID, name: str, *, template: str | None = None, **fields: Any
    ) -> Tracker:
        """A tracker; `fields` override the template's kind, unit and bounds."""
        unknown = set(fields) - (TRACKER_FIELDS - {"name", "archived"})
        if unknown:
            raise InvalidTrackerError(f"a tracker has no field {sorted(unknown)[0]}")
        base: dict[str, Any] = {}
        if template is not None:
            preset = TEMPLATES.get(template)
            if preset is None:
                raise InvalidTrackerError(f"there is no template {template!r}")
            base = {
                "kind": preset.kind,
                "unit": preset.unit,
                "min_value": preset.min_value,
                "max_value": preset.max_value,
            }
        merged = base | fields
        if merged.get("kind") is None:
            raise InvalidTrackerError("give a kind or a template")
        if merged.get("unit") is None:
            raise InvalidTrackerError("give a unit, such as a currency")
        values: dict[str, Any] = {
            "name": _name(name),
            "kind": _kind(merged["kind"]),
            "unit": _unit(merged["unit"]),
            "min_value": _bound(merged.get("min_value"), "min_value"),
            "max_value": _bound(merged.get("max_value"), "max_value"),
            "schedule": _schedule(merged.get("schedule")),
            **_target_columns(_target(merged.get("target"))),
        }
        _check_tracker(values)
        await self._unique(actor, values["name"])
        tracker = Tracker(owner_id=actor, **values)
        self.session.add(tracker)
        await self._done()
        return tracker

    async def trackers(
        self,
        actor: uuid.UUID,
        *,
        kind: TrackerKind | None = None,
        archived: bool | None = False,
    ) -> list[Tracker]:
        """The actor's trackers by name; `archived=None` lists both."""
        query = select(Tracker).where(Tracker.owner_id == actor)
        if kind is not None:
            query = query.where(Tracker.kind == kind)
        if archived is not None:
            query = query.where(Tracker.archived.is_(archived))
        found = (await self.session.scalars(query)).all()
        # Sorted here for the same reason names are compared here: the
        # database's collation depends on its locale.
        return sorted(found, key=lambda t: (t.name.casefold(), t.id))

    async def get_tracker(
        self, actor: uuid.UUID, tracker_id: uuid.UUID, *, lock: bool = False
    ) -> Tracker:
        query = select(Tracker).where(Tracker.id == tracker_id, Tracker.owner_id == actor)
        if lock:
            query = query.with_for_update()
        tracker = await self.session.scalar(query)
        if tracker is None:
            raise NotFoundError("tracker not found")
        return tracker

    async def update_tracker(
        self, actor: uuid.UUID, tracker_id: uuid.UUID, changes: Mapping[str, Any]
    ) -> Tracker:
        unknown = set(changes) - TRACKER_FIELDS
        if unknown:
            raise InvalidTrackerError(f"a tracker has no field {sorted(unknown)[0]}")
        tracker = await self.get_tracker(actor, tracker_id, lock=True)
        # Everything is checked before the tracker changes, so a refused update
        # leaves it untouched.
        values: dict[str, Any] = {}
        if "name" in changes:
            values["name"] = _name(changes["name"])
        if "kind" in changes:
            values["kind"] = _kind(changes["kind"])
        if "unit" in changes:
            values["unit"] = _unit(changes["unit"])
        if "min_value" in changes:
            values["min_value"] = _bound(changes["min_value"], "min_value")
        if "max_value" in changes:
            values["max_value"] = _bound(changes["max_value"], "max_value")
        if "target" in changes:
            values.update(_target_columns(_target(changes["target"])))
        if "schedule" in changes:
            values["schedule"] = _schedule(changes["schedule"])
        if "archived" in changes:
            if not isinstance(changes["archived"], bool):
                raise InvalidTrackerError("archived is true or false")
            values["archived"] = changes["archived"]
        _check_tracker(
            {
                name: values.get(name, getattr(tracker, name))
                for name in ("min_value", "max_value", "schedule", "target_period")
            }
        )
        if "name" in values and values["name"] != tracker.name:
            await self._unique(actor, values["name"], besides=tracker.id)
        for name, value in values.items():
            setattr(tracker, name, value)
        tracker.updated_at = _now()
        await self._done()
        return tracker

    async def delete_tracker(self, actor: uuid.UUID, tracker_id: uuid.UUID) -> None:
        tracker = await self.get_tracker(actor, tracker_id, lock=True)
        await self.session.execute(delete(Entry).where(Entry.tracker_id == tracker.id))
        await self.session.delete(tracker)
        await self._done()

    # --------------------------------------------------------------- entries

    async def log(
        self,
        actor: uuid.UUID,
        tracker_id: uuid.UUID,
        value: object,
        *,
        at: datetime | None = None,
        note: str = "",
        category: str | None = None,
    ) -> Entry:
        tracker = await self.get_tracker(actor, tracker_id)
        if tracker.archived:
            raise ArchivedError("the tracker is archived; restore it to log entries")
        number = _value(value, "the value")
        _check_value(tracker, number)
        entry = Entry(
            tracker_id=tracker.id,
            at=_now() if at is None else _at(at),
            value=number,
            note=_note(note),
            category=_category(category),
        )
        self.session.add(entry)
        await self._done()
        return entry

    async def entries(
        self,
        actor: uuid.UUID,
        tracker_id: uuid.UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        category: str | None = None,
        before: uuid.UUID | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[Entry]:
        """Newest first by `at`; `start` is inclusive, `end` exclusive."""
        tracker = await self.get_tracker(actor, tracker_id)
        query = select(Entry).where(Entry.tracker_id == tracker.id)
        if start is not None:
            query = query.where(Entry.at >= _at(start))
        if end is not None:
            query = query.where(Entry.at < _at(end))
        if category is not None:
            query = query.where(Entry.category == _category(category))
        if before is not None:
            cursor = await self.session.scalar(
                select(Entry.at).where(Entry.id == before, Entry.tracker_id == tracker.id)
            )
            if cursor is None:
                raise InvalidEntryError("before names no entry of this tracker")
            query = query.where(or_(Entry.at < cursor, and_(Entry.at == cursor, Entry.id < before)))
        query = query.order_by(Entry.at.desc(), Entry.id.desc()).limit(max(1, min(limit, MAX_PAGE)))
        return list((await self.session.scalars(query)).all())

    async def _entry(
        self, actor: uuid.UUID, tracker_id: uuid.UUID, entry_id: uuid.UUID
    ) -> tuple[Tracker, Entry]:
        tracker = await self.get_tracker(actor, tracker_id)
        entry = await self.session.scalar(
            select(Entry)
            .where(Entry.id == entry_id, Entry.tracker_id == tracker.id)
            .with_for_update()
        )
        if entry is None:
            raise NotFoundError("entry not found")
        return tracker, entry

    async def update_entry(
        self,
        actor: uuid.UUID,
        tracker_id: uuid.UUID,
        entry_id: uuid.UUID,
        changes: Mapping[str, Any],
    ) -> Entry:
        unknown = set(changes) - ENTRY_FIELDS
        if unknown:
            raise InvalidEntryError(f"an entry has no field {sorted(unknown)[0]}")
        tracker, entry = await self._entry(actor, tracker_id, entry_id)
        values: dict[str, Any] = {}
        if "at" in changes:
            values["at"] = _at(changes["at"])
        if "value" in changes:
            values["value"] = _value(changes["value"], "the value")
            _check_value(tracker, values["value"])
        if "note" in changes:
            values["note"] = _note(changes["note"])
        if "category" in changes:
            values["category"] = _category(changes["category"])
        for name, value in values.items():
            setattr(entry, name, value)
        entry.updated_at = _now()
        await self._done()
        return entry

    async def delete_entry(
        self, actor: uuid.UUID, tracker_id: uuid.UUID, entry_id: uuid.UUID
    ) -> None:
        _, entry = await self._entry(actor, tracker_id, entry_id)
        await self.session.delete(entry)
        await self._done()

    # ----------------------------------------------------------------- stats

    async def _buckets(
        self, tracker_id: uuid.UUID, zone: str, period: Period, begins: datetime, ends: datetime
    ) -> dict[date, _Row]:
        """Aggregates per local period with entries, keyed by the period's first day."""
        # The period is one of three fixed words, so it can be inlined; the zone
        # stays a parameter. Grouping over a subquery keeps the parameter out of
        # GROUP BY, where Postgres would not match it to the select list.
        local = func.timezone(zone, Entry.at)
        inner = (
            select(
                func.date_trunc(literal_column(f"'{period.value}'"), local).label("bucket"),
                Entry.value,
            )
            .where(Entry.tracker_id == tracker_id, Entry.at >= begins, Entry.at < ends)
            .subquery()
        )
        rows = await self.session.execute(
            select(
                inner.c.bucket,
                func.count(),
                func.coalesce(func.sum(inner.c.value), 0),
                func.avg(inner.c.value),
                func.min(inner.c.value),
                func.max(inner.c.value),
            ).group_by(inner.c.bucket)
        )
        return {
            bucket.date(): _Row(count, Decimal(total), _round(average), low, high)
            for bucket, count, total, average, low, high in rows
        }

    async def _categories(
        self, tracker_id: uuid.UUID, begins: datetime, ends: datetime
    ) -> list[CategoryTotal]:
        rows = await self.session.execute(
            select(Entry.category, func.count(), func.sum(Entry.value))
            .where(Entry.tracker_id == tracker_id, Entry.at >= begins, Entry.at < ends)
            .group_by(Entry.category)
            .order_by(func.sum(Entry.value).desc(), Entry.category)
        )
        return [CategoryTotal(category, count, Decimal(total)) for category, count, total in rows]

    async def streak(
        self, actor: uuid.UUID, tracker_id: uuid.UUID, *, now: datetime | None = None
    ) -> tuple[int, Period]:
        tracker = await self.get_tracker(actor, tracker_id)
        tz = await user_zone(self.session, actor)
        return await self._streak(tracker, tz, now or _now())

    async def _streak(self, tracker: Tracker, tz: ZoneInfo, now: datetime) -> tuple[int, Period]:
        target = target_of(tracker)
        period = target.period if target else Period.DAY
        today = now.astimezone(tz).date()
        current = period_start(today, period)
        created = period_start(tracker.created_at.astimezone(tz).date(), period)
        first = max(created, shift(current, period, MAX_STREAK - 1))
        if first > current:
            return 0, period
        rows = await self._buckets(
            tracker.id,
            str(tz),
            period,
            _midnight(first, tz),
            _midnight(next_period(current, period), tz),
        )
        due: set[date] | None = None
        if tracker.schedule is not None:
            # The schedule starts on the day the tracker was created, which fixes
            # the phase of rules such as every other day.
            rule = recurrence.parse(
                tracker.schedule, datetime.combine(created, time(), tz).astimezone(UTC), tz
            )
            window = rule.between(
                datetime.combine(first, time(), tz),
                datetime.combine(current, time(23, 59, 59), tz),
                inc=True,
            )
            due = {moment.date() for moment in window}
        count = 0
        day = current
        while day >= first:
            if due is None or day in due:
                row = rows.get(day)
                met = is_met(row.sum if row else Decimal(0), row.count if row else 0, target)
                if met:
                    count += 1
                elif day != current:
                    break
            day = shift(day, period, 1)
        return count, period

    async def stats(
        self,
        actor: uuid.UUID,
        tracker_id: uuid.UUID,
        *,
        period: Period | None = None,
        start: date | None = None,
        end: date | None = None,
        now: datetime | None = None,
    ) -> Stats:
        """Stats over whole local periods from `start` to `end`, both inclusive."""
        tracker = await self.get_tracker(actor, tracker_id)
        tz = await user_zone(self.session, actor)
        now = now or _now()
        target = target_of(tracker)
        period = period or (target.period if target else Period.DAY)
        last = end or now.astimezone(tz).date()
        first = start or shift(period_start(last, period), period, DEFAULT_PERIODS[period] - 1)
        if first > last:
            raise InvalidTrackerError("the start is after the end")
        starts = periods(first, last, period)
        if len(starts) > MAX_PERIODS:
            raise InvalidTrackerError(f"stats cover at most {MAX_PERIODS} periods")
        begins = _midnight(starts[0], tz)
        ends = _midnight(next_period(starts[-1], period), tz)
        rows = await self._buckets(tracker.id, str(tz), period, begins, ends)
        # A target applies per its own period, so buckets of another length
        # are not judged against it.
        judged = target if target is None or target.period is period else None
        buckets: list[Bucket] = []
        for day in starts:
            row = rows.get(day)
            if row is None:
                row = _Row(0, Decimal(0), None, None, None)
            met = is_met(row.sum, row.count, judged) if target is None or judged else None
            buckets.append(Bucket(day, row.count, row.sum, row.average, row.min, row.max, met))
        count = sum(b.count for b in buckets)
        total = sum((b.sum for b in buckets), Decimal(0))
        lows = [b.min for b in buckets if b.min is not None]
        highs = [b.max for b in buckets if b.max is not None]
        streak, streak_period = await self._streak(tracker, tz, now)
        return Stats(
            period=period,
            time_zone=str(tz),
            start=starts[0],
            end=next_period(starts[-1], period) - timedelta(days=1),
            count=count,
            sum=total,
            average=_round(total / count) if count else None,
            min=min(lows) if lows else None,
            max=max(highs) if highs else None,
            per_period=_round(total / len(starts)),
            buckets=buckets,
            categories=await self._categories(tracker.id, begins, ends),
            streak=streak,
            streak_period=streak_period,
        )
