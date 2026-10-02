"""Agent tools of the trackers domain (docs/spec/domains/trackers.md#agent-tools).

Every call goes through `TrackersService` with `commit=False`; trackers are
private to their owner, so no call is ever `external`. Under the `aggregates`
exposure (ADR 0007) health and finance trackers show sums, averages and
streaks only, never single entries.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from titan.domains.autonomy.errors import UndoConflictError
from titan.domains.autonomy.models import ActionClass
from titan.domains.autonomy.tools import (
    Change,
    Exposure,
    RunFn,
    ToolContext,
    ToolResult,
    ToolSpec,
    guarded,
)
from titan.domains.calendar.zones import parse_local, show_local, user_zone
from titan.domains.trackers.errors import (
    InvalidEntryError,
    InvalidTrackerError,
    NotFoundError,
    TrackersError,
)
from titan.domains.trackers.models import Entry, Period, Tracker, TrackerKind
from titan.domains.trackers.service import (
    CATEGORY_LENGTH,
    MAX_PAGE,
    NAME_LENGTH,
    NOTE_LENGTH,
    UNIT_LENGTH,
    TrackersService,
    target_of,
)
from titan.domains.trackers.templates import TEMPLATES

DOMAIN = "trackers"
SENSITIVE = (TrackerKind.HEALTH, TrackerKind.FINANCE)
DEFAULT_ENTRIES = 20

# ------------------------------------------------------------------ helpers


def _service(context: ToolContext) -> TrackersService:
    return TrackersService(context.session, commit=False)


def _guard(run: RunFn) -> RunFn:
    return guarded(run, (TrackersError,))


def _plain(value: Decimal | None) -> str:
    return "" if value is None else format(value.normalize(), "f")


def _when(value: object, zone: ZoneInfo) -> datetime:
    try:
        return parse_local(str(value), zone)
    except ValueError:
        raise InvalidEntryError(f"{value} is not an ISO 8601 date and time") from None


def _entry_id(args: Mapping[str, Any]) -> uuid.UUID:
    try:
        return uuid.UUID(str(args["entry_id"]))
    except ValueError:
        raise InvalidEntryError("entry_id is not an id") from None


def _day(value: object) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise InvalidTrackerError(f"{value} is not a date (YYYY-MM-DD)") from None


async def _tracker(context: ToolContext, ref: object, *, lock: bool = False) -> Tracker:
    """A tracker by id, or by name ignoring case, as people name them."""
    service = _service(context)
    text = str(ref).strip()
    try:
        tracker_id = uuid.UUID(text)
    except ValueError:
        for tracker in await service.trackers(context.user_id, archived=None):
            if tracker.name.casefold() == text.casefold():
                return await service.get_tracker(context.user_id, tracker.id, lock=lock)
        raise NotFoundError(f"there is no tracker called {text}") from None
    return await service.get_tracker(context.user_id, tracker_id, lock=lock)


def _hides_entries(context: ToolContext, tracker: Tracker) -> bool:
    return context.exposure is Exposure.AGGREGATES and tracker.kind in SENSITIVE


def _target_text(tracker: Tracker) -> str:
    target = target_of(tracker)
    if target is None:
        return ""
    direction = "at least" if target.direction.value == "at_least" else "at most"
    return f"target {direction} {_plain(target.value)} {tracker.unit} per {target.period.value}"


def _tracker_line(tracker: Tracker) -> str:
    parts = [f"- {tracker.id} {tracker.name} ({tracker.kind.value}, {tracker.unit})"]
    if tracker.min_value is not None or tracker.max_value is not None:
        parts.append(f"values {_plain(tracker.min_value)}..{_plain(tracker.max_value)}")
    if target := _target_text(tracker):
        parts.append(target)
    if tracker.schedule:
        parts.append(f"due {tracker.schedule}")
    if tracker.archived:
        parts.append("archived")
    return " · ".join(parts)


# ------------------------------------------------------------ snapshots


def tracker_state(tracker: Tracker) -> dict[str, Any]:
    target = target_of(tracker)
    return {
        "name": tracker.name,
        "kind": tracker.kind.value,
        "unit": tracker.unit,
        "min_value": _plain(tracker.min_value) or None,
        "max_value": _plain(tracker.max_value) or None,
        "target": None
        if target is None
        else {
            "value": _plain(target.value),
            "period": target.period.value,
            "direction": target.direction.value,
        },
        "schedule": tracker.schedule,
        "archived": tracker.archived,
    }


def entry_state(entry: Entry) -> dict[str, Any]:
    return {
        "tracker_id": str(entry.tracker_id),
        "at": entry.at.astimezone(UTC).isoformat(),
        "value": _plain(entry.value),
        "note": entry.note,
        "category": entry.category,
    }


async def _unchanged_tracker(context: ToolContext, change: Change) -> Tracker:
    try:
        tracker = await _service(context).get_tracker(
            context.user_id, uuid.UUID(change.entity_id), lock=True
        )
    except TrackersError:
        raise UndoConflictError("the tracker no longer exists") from None
    if tracker_state(tracker) != change.after:
        raise UndoConflictError("the tracker was changed again since")
    return tracker


async def _unchanged_entry(context: ToolContext, change: Change) -> Entry:
    entry = await context.session.scalar(
        select(Entry)
        .join(Tracker, Tracker.id == Entry.tracker_id)
        .where(Entry.id == uuid.UUID(change.entity_id), Tracker.owner_id == context.user_id)
        .with_for_update(of=Entry)
    )
    if entry is None:
        raise UndoConflictError("the entry no longer exists")
    if entry_state(entry) != change.after:
        raise UndoConflictError("the entry was changed again since")
    return entry


async def _tracker_name(context: ToolContext, args: Mapping[str, Any]) -> str | None:
    try:
        return (await _tracker(context, args["tracker"])).name
    except (TrackersError, KeyError):
        return None


# --------------------------------------------------------------- reading


async def _list_trackers(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    kind = TrackerKind(args["kind"]) if args.get("kind") else None
    archived = None if args.get("include_archived") else False
    trackers = await _service(context).trackers(context.user_id, kind=kind, archived=archived)
    if not trackers:
        return ToolResult("No trackers yet.")
    return ToolResult("Trackers:\n" + "\n".join(_tracker_line(t) for t in trackers))


async def _stats(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    tracker = await _tracker(context, args["tracker"])
    stats = await _service(context).stats(
        context.user_id,
        tracker.id,
        period=Period(args["period"]) if args.get("period") else None,
        start=_day(args.get("from")),
        end=_day(args.get("to")),
    )
    hide = _hides_entries(context, tracker)
    unit = tracker.unit
    lines = [
        f"{tracker.name}, {stats.start} to {stats.end} by {stats.period.value} "
        f"({stats.time_zone}):",
        f"total {_plain(stats.sum)} {unit} in {stats.count} entries, "
        f"{_plain(stats.per_period)} per {stats.period.value}, "
        f"average entry {_plain(stats.average) or '-'}",
    ]
    if not hide and stats.count:
        lines.append(f"lowest entry {_plain(stats.min)}, highest {_plain(stats.max)}")
    lines.append(f"streak: {stats.streak} {stats.streak_period.value}(s)")
    if target := _target_text(tracker):
        lines.append(target)
    for bucket in stats.buckets:
        met = "" if bucket.met is None else (" met" if bucket.met else " not met")
        lines.append(f"  {bucket.start}: {_plain(bucket.sum)} ({bucket.count}){met}")
    named = [c for c in stats.categories if c.category]
    if named:
        lines.append("by category: " + ", ".join(f"{c.category} {_plain(c.sum)}" for c in named))
    return ToolResult("\n".join(lines))


async def _list_entries(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    tracker = await _tracker(context, args["tracker"])
    if _hides_entries(context, tracker):
        return ToolResult(
            f"Single {tracker.kind.value} entries are not shown here; use tracker_stats.",
            is_error=True,
        )
    zone = await user_zone(context.session, context.user_id)
    entries = await _service(context).entries(
        context.user_id,
        tracker.id,
        start=_when(args["from"], zone) if args.get("from") else None,
        end=_when(args["to"], zone) if args.get("to") else None,
        category=args.get("category"),
        limit=int(args.get("limit", DEFAULT_ENTRIES)),
    )
    if not entries:
        return ToolResult(f"No entries in {tracker.name}.")
    lines = []
    for entry in entries:
        line = f"- {entry.id} {show_local(entry.at, zone)}: {_plain(entry.value)} {tracker.unit}"
        if entry.category:
            line += f" [{entry.category}]"
        if entry.note:
            line += f" “{entry.note}”"
        lines.append(line)
    return ToolResult(f"{tracker.name}, newest first:\n" + "\n".join(lines))


# --------------------------------------------------------------- trackers

_VALUE = {"type": ["number", "string"], "description": "a decimal number"}
_TARGET = {
    "type": ["object", "null"],
    "properties": {
        "value": _VALUE,
        "period": {"enum": [p.value for p in Period]},
        "direction": {"enum": ["at_least", "at_most"]},
    },
    "required": ["value", "period"],
    "additionalProperties": False,
}
_TRACKER_FIELDS: dict[str, Any] = {
    "name": {"type": "string", "minLength": 1, "maxLength": NAME_LENGTH},
    "kind": {"enum": [k.value for k in TrackerKind]},
    "unit": {"type": "string", "minLength": 1, "maxLength": UNIT_LENGTH},
    "min_value": {"type": ["number", "string", "null"]},
    "max_value": {"type": ["number", "string", "null"]},
    "target": _TARGET,
    "schedule": {
        "type": ["string", "null"],
        "description": "RRULE of the days a habit is due, e.g. FREQ=WEEKLY;BYDAY=MO,WE,FR",
    },
}


async def _create_tracker(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    fields = {k: v for k, v in args.items() if k in _TRACKER_FIELDS and k != "name"}
    tracker = await _service(context).create_tracker(
        context.user_id, args["name"], template=args.get("template"), **fields
    )
    return ToolResult(
        f"Created the tracker “{tracker.name}” ({tracker.id}).",
        Change("tracker", str(tracker.id), {}, tracker_state(tracker)),
    )


async def _undo_create_tracker(context: ToolContext, change: Change) -> None:
    tracker = await _unchanged_tracker(context, change)
    if await context.session.scalar(
        select(Entry.id).where(Entry.tracker_id == tracker.id).limit(1)
    ):
        raise UndoConflictError("the tracker has entries now")
    await context.session.delete(tracker)
    await context.session.flush()


async def _update_tracker(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    tracker = await _tracker(context, args["tracker"])
    before = tracker_state(tracker)
    changes = {k: v for k, v in args.items() if k in _TRACKER_FIELDS or k == "archived"}
    tracker = await _service(context).update_tracker(context.user_id, tracker.id, changes)
    return ToolResult(
        f"Updated the tracker “{tracker.name}”.",
        Change("tracker", str(tracker.id), before, tracker_state(tracker)),
    )


async def _undo_update_tracker(context: ToolContext, change: Change) -> None:
    tracker = await _unchanged_tracker(context, change)
    try:
        await _service(context).update_tracker(context.user_id, tracker.id, change.before)
    except TrackersError as exc:
        raise UndoConflictError(str(exc)) from None


async def _delete_tracker(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    tracker = await _tracker(context, args["tracker"])
    before, name = tracker_state(tracker), tracker.name
    await _service(context).delete_tracker(context.user_id, tracker.id)
    return ToolResult(
        f"Deleted the tracker “{name}” with its entries.",
        Change("tracker", str(tracker.id), before, {}),
    )


# ---------------------------------------------------------------- entries

_ENTRY_FIELDS: dict[str, Any] = {
    "value": _VALUE,
    "at": {
        "type": "string",
        "description": "ISO 8601, by default now; without an offset in the user's time zone",
    },
    "note": {"type": "string", "maxLength": NOTE_LENGTH},
    "category": {
        "type": ["string", "null"],
        "maxLength": CATEGORY_LENGTH,
        "description": "for finance, such as groceries or transport",
    },
}
_TRACKER_REF = {
    "tracker": {"type": "string", "minLength": 1, "description": "the tracker's id or name"}
}


async def _entry_changes(context: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    changes = {k: v for k, v in args.items() if k in _ENTRY_FIELDS}
    if "at" in changes:
        changes["at"] = _when(changes["at"], await user_zone(context.session, context.user_id))
    return changes


async def _log_entry(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    tracker = await _tracker(context, args["tracker"])
    changes = await _entry_changes(context, args)
    entry = await _service(context).log(
        context.user_id,
        tracker.id,
        changes.pop("value"),
        at=changes.get("at"),
        note=changes.get("note", ""),
        category=changes.get("category"),
    )
    return ToolResult(
        f"Logged {_plain(entry.value)} {tracker.unit} in “{tracker.name}” ({entry.id}).",
        Change("tracker_entry", str(entry.id), {}, entry_state(entry)),
    )


async def _undo_log_entry(context: ToolContext, change: Change) -> None:
    entry = await _unchanged_entry(context, change)
    await context.session.delete(entry)
    await context.session.flush()


async def _update_entry(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    tracker = await _tracker(context, args["tracker"])
    service = _service(context)
    entry_id = _entry_id(args)
    current = await context.session.get(Entry, entry_id)
    if current is None or current.tracker_id != tracker.id:
        raise NotFoundError("entry not found")
    before = entry_state(current)
    entry = await service.update_entry(
        context.user_id, tracker.id, entry_id, await _entry_changes(context, args)
    )
    return ToolResult(
        f"Updated the entry in “{tracker.name}”.",
        Change("tracker_entry", str(entry.id), before, entry_state(entry)),
    )


async def _undo_update_entry(context: ToolContext, change: Change) -> None:
    entry = await _unchanged_entry(context, change)
    before = change.before
    entry.at = datetime.fromisoformat(before["at"])
    entry.value = Decimal(before["value"])
    entry.note = before["note"]
    entry.category = before["category"]
    entry.updated_at = datetime.now(UTC)
    await context.session.flush()


async def _delete_entry(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    tracker = await _tracker(context, args["tracker"])
    entry_id = _entry_id(args)
    entry = await context.session.get(Entry, entry_id)
    if entry is None or entry.tracker_id != tracker.id:
        raise NotFoundError("entry not found")
    before = entry_state(entry)
    await _service(context).delete_entry(context.user_id, tracker.id, entry_id)
    return ToolResult(
        f"Deleted the entry from “{tracker.name}”.",
        Change("tracker_entry", str(entry_id), before, {}),
    )


# ------------------------------------------------------------- summaries


def _in(name: str | None) -> str:
    return f" in “{name}”" if name else ""


async def _describe_log(context: ToolContext, args: Mapping[str, Any]) -> str:
    return f"Log {args.get('value', '?')}{_in(await _tracker_name(context, args))}"


async def _describe_update_tracker(context: ToolContext, args: Mapping[str, Any]) -> str:
    name = await _tracker_name(context, args) or args.get("tracker", "?")
    changed = [k for k in args if k != "tracker"]
    return f"Change the tracker “{name}”: {', '.join(changed) or 'nothing'}"


async def _describe_update_entry(context: ToolContext, args: Mapping[str, Any]) -> str:
    changed = [k for k in args if k not in ("tracker", "entry_id")]
    return f"Change an entry{_in(await _tracker_name(context, args))}: {', '.join(changed)}"


async def _describe_delete_entry(context: ToolContext, args: Mapping[str, Any]) -> str:
    return f"Delete an entry{_in(await _tracker_name(context, args))}"


async def _describe_delete_tracker(context: ToolContext, args: Mapping[str, Any]) -> str:
    name = await _tracker_name(context, args) or args.get("tracker", "?")
    return f"Delete the tracker “{name}” with all its entries"


LIST_TRACKERS = ToolSpec(
    domain=DOMAIN,
    name="list_trackers",
    description="List the user's trackers (habits, health, finance, custom) by name.",
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"enum": [k.value for k in TrackerKind]},
            "include_archived": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    run=_guard(_list_trackers),
    summarize=lambda args: "List trackers",
)

TRACKER_STATS = ToolSpec(
    domain=DOMAIN,
    name="tracker_stats",
    description=(
        "Totals, averages, the streak and whether each period met the target, per "
        "day, week or month between two local dates (YYYY-MM-DD). By default the "
        "target's period, over the last 30 days, 12 weeks or 12 months."
    ),
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": _TRACKER_REF
        | {
            "period": {"enum": [p.value for p in Period]},
            "from": {"type": "string"},
            "to": {"type": "string"},
        },
        "required": ["tracker"],
        "additionalProperties": False,
    },
    run=_guard(_stats),
    summarize=lambda args: "Show tracker stats",
)

LIST_ENTRIES = ToolSpec(
    domain=DOMAIN,
    name="list_entries",
    description="A tracker's entries, newest first; from and to are times.",
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": _TRACKER_REF
        | {
            "from": {"type": "string"},
            "to": {"type": "string"},
            "category": {"type": "string", "maxLength": CATEGORY_LENGTH},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        },
        "required": ["tracker"],
        "additionalProperties": False,
    },
    run=_guard(_list_entries),
    summarize=lambda args: "List tracker entries",
)

CREATE_TRACKER = ToolSpec(
    domain=DOMAIN,
    name="create_tracker",
    description=(
        "Create a tracker, named in the user's language. A template fills in the "
        f"kind, unit and bounds: {', '.join(TEMPLATES)}; expense and income need a "
        "unit (the currency). Without a template give kind and unit."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _TRACKER_FIELDS | {"template": {"enum": list(TEMPLATES)}},
        "required": ["name"],
        "additionalProperties": False,
    },
    run=_guard(_create_tracker),
    summarize=lambda args: f"Create the tracker “{args.get('name', '')}”",
    undo=_undo_create_tracker,
)

UPDATE_TRACKER = ToolSpec(
    domain=DOMAIN,
    name="update_tracker",
    description=(
        "Change a tracker: name, unit, bounds, target, schedule, or archived "
        "(true stops new entries and hides it; false restores it)."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _TRACKER_REF | _TRACKER_FIELDS | {"archived": {"type": "boolean"}},
        "required": ["tracker"],
        "additionalProperties": False,
    },
    run=_guard(_update_tracker),
    summarize=lambda args: "Change a tracker",
    undo=_undo_update_tracker,
    describe=_describe_update_tracker,
)

DELETE_TRACKER = ToolSpec(
    domain=DOMAIN,
    name="delete_tracker",
    description="Delete a tracker and all its entries. Prefer archiving it.",
    action_class=ActionClass.DESTRUCTIVE,
    input_schema={
        "type": "object",
        "properties": _TRACKER_REF,
        "required": ["tracker"],
        "additionalProperties": False,
    },
    run=_guard(_delete_tracker),
    summarize=lambda args: f"Delete the tracker “{args.get('tracker', '')}”",
    describe=_describe_delete_tracker,
)

LOG_ENTRY = ToolSpec(
    domain=DOMAIN,
    name="log_entry",
    description=(
        "Log a value in a tracker, such as 72.4 kg, 1 glass or 23.40 spent on "
        "groceries. Use list_trackers to find it, or create_tracker first."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _TRACKER_REF | _ENTRY_FIELDS,
        "required": ["tracker", "value"],
        "additionalProperties": False,
    },
    run=_guard(_log_entry),
    summarize=lambda args: f"Log {args.get('value', '?')} in “{args.get('tracker', '')}”",
    undo=_undo_log_entry,
    describe=_describe_log,
)

UPDATE_ENTRY = ToolSpec(
    domain=DOMAIN,
    name="update_entry",
    description="Correct an entry's value, time, note or category.",
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _TRACKER_REF | {"entry_id": {"type": "string"}} | _ENTRY_FIELDS,
        "required": ["tracker", "entry_id"],
        "additionalProperties": False,
    },
    run=_guard(_update_entry),
    summarize=lambda args: "Change a tracker entry",
    undo=_undo_update_entry,
    describe=_describe_update_entry,
)

DELETE_ENTRY = ToolSpec(
    domain=DOMAIN,
    name="delete_entry",
    description="Delete one entry of a tracker.",
    action_class=ActionClass.DESTRUCTIVE,
    input_schema={
        "type": "object",
        "properties": _TRACKER_REF | {"entry_id": {"type": "string"}},
        "required": ["tracker", "entry_id"],
        "additionalProperties": False,
    },
    run=_guard(_delete_entry),
    summarize=lambda args: "Delete a tracker entry",
    describe=_describe_delete_entry,
)

TOOLS = (
    LIST_TRACKERS,
    TRACKER_STATS,
    LIST_ENTRIES,
    CREATE_TRACKER,
    UPDATE_TRACKER,
    DELETE_TRACKER,
    LOG_ENTRY,
    UPDATE_ENTRY,
    DELETE_ENTRY,
)
