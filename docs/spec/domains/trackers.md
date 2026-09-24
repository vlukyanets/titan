# Domain: trackers (habits, health, finance)

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

Trackers are structured time series. They share one data model, so adding a new
kind of tracker needs no new code.

## Entities

| Entity | Key fields |
|---|---|
| `Tracker` | id, owner, name, kind (`habit`, `health`, `finance`, `custom`), unit (`count`, `kg`, `min`, `EUR`, …), target? (for example "8 glasses per day"), schedule (RRULE)? |
| `Entry` | id, tracker_id, at, value (number), note?, category? (finance: `groceries`, `transport`, …) |

Entries of `health` and `finance` trackers are stored in their own tables
(`health_entries`, `finance_entries`), separate from the other entries. Those
tables belong to the `sensitive` replication set and exist only on trusted
nodes ([ADR 0007](../../adr/0007-sensitive-data-protection.md)).

Built-in templates for v1: habit check-in (`count`), weight (`kg`), sleep
(`h`), workout (`min`), mood (1–5), expense (currency), income (currency).

## Agent tools

| Tool | Action class |
|---|---|
| `trackers.list` / `trackers.stats` (sum, average, streak over a period) | `read` |
| `trackers.create` / `entries.log` / `entries.update` | `write-internal` |
| `trackers.delete` / `entries.delete` | `destructive` |

Exposure to Claude ([ADR 0007](../../adr/0007-sensitive-data-protection.md)):
in a user's own chat, tools return their own `health` and `finance` entries in
full. In scheduled workflows they return only aggregates (sums, averages,
streaks). Users can change both settings per domain.

## Acceptance criteria (v1)

- Logging from chat in natural language works ("spent 23.40 on groceries",
  "slept 7 hours").
- Habit streaks and simple period stats (week and month totals and averages) are
  available on every surface.
- Health and finance entries are private to their owner and cannot be shared in
  v1.
- On an untrusted node, health and finance requests return "not available on
  this node" instead of empty data.
