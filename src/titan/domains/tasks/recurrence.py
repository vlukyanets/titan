"""RRULE checks and the next occurrence of a recurring task."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo

from dateutil.rrule import rrule, rrulestr

from titan.domains.tasks.errors import InvalidRecurrenceError

MAX_LENGTH = 200
_FREQUENCIES = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
# How far ahead the next occurrence is looked for. With at most daily rules this
# bounds the work, so a rule that never matches (such as 30 February) cannot spin.
HORIZON = timedelta(days=5 * 366)


def normalize(value: str) -> str:
    """The rule as stored: upper case, without an `RRULE:` prefix."""
    rule = value.strip().upper().removeprefix("RRULE:")
    if not rule:
        raise InvalidRecurrenceError("the recurrence rule is empty")
    if len(rule) > MAX_LENGTH:
        raise InvalidRecurrenceError(f"the recurrence rule is longer than {MAX_LENGTH} characters")
    if "\n" in rule or "DTSTART" in rule:
        raise InvalidRecurrenceError("give only the RRULE; due_at is its start")
    parts = dict(part.partition("=")[::2] for part in rule.split(";"))
    if "COUNT" in parts:
        raise InvalidRecurrenceError("COUNT is not supported; end the rule with UNTIL")
    if parts.get("FREQ") not in _FREQUENCIES:
        raise InvalidRecurrenceError("FREQ must be DAILY, WEEKLY, MONTHLY or YEARLY")
    parse(rule, datetime(2000, 1, 1, tzinfo=UTC))
    return rule


def parse(rule: str, start: datetime, tz: tzinfo = UTC) -> rrule:
    """The rule starting at `start`, repeating at the same local time in `tz`."""
    try:
        parsed = rrulestr(rule, dtstart=start.astimezone(tz))
    except (ValueError, TypeError) as exc:
        raise InvalidRecurrenceError(f"not a valid RRULE: {exc}") from None
    if not isinstance(parsed, rrule):
        raise InvalidRecurrenceError("give a single RRULE")
    return parsed


def next_occurrence(
    rule: str, due_at: datetime, completed_at: datetime, tz: tzinfo = UTC
) -> datetime | None:
    """The first occurrence after both the old due date and the completion, in UTC.

    Repeats keep their local time in `tz`, the owner's zone, across daylight
    saving changes. Occurrences missed while the task was open are skipped.
    `None` when the rule has ended, or has no occurrence within `HORIZON`.
    """
    parsed = parse(rule, due_at, tz)
    after = max(due_at, completed_at).astimezone(UTC)
    upcoming = parsed.between(after, after + HORIZON, inc=False)
    return upcoming[0].astimezone(UTC) if upcoming else None
