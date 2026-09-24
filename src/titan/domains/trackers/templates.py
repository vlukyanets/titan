"""Built-in tracker templates. The caller always names the tracker."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from titan.domains.trackers.models import TrackerKind


@dataclass(frozen=True)
class Template:
    id: str
    kind: TrackerKind
    # None: the caller gives the unit, such as a currency.
    unit: str | None
    min_value: Decimal | None = None
    max_value: Decimal | None = None


_ZERO = Decimal(0)

TEMPLATES: dict[str, Template] = {
    t.id: t
    for t in (
        Template("habit", TrackerKind.HABIT, "count", _ZERO),
        Template("weight", TrackerKind.HEALTH, "kg", _ZERO),
        Template("sleep", TrackerKind.HEALTH, "h", _ZERO, Decimal(24)),
        Template("workout", TrackerKind.HEALTH, "min", _ZERO),
        Template("mood", TrackerKind.HEALTH, "score", Decimal(1), Decimal(5)),
        Template("expense", TrackerKind.FINANCE, None, _ZERO),
        Template("income", TrackerKind.FINANCE, None, _ZERO),
    )
}
