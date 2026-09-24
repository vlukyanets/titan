# Domain: trackers (habits, health, finance)

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

Trackers are structured time series. They share one data model, so adding a new
kind of tracker needs no new code.

## Entities

| Entity | Key fields |
|---|---|
| `Tracker` | id, owner, name, kind (`habit`, `health`, `finance`, `custom`), unit (`count`, `kg`, `min`, `EUR`, …), target? (for example "8 glasses per day"), schedule (RRULE)? |
| `Entry` | id, tracker_id, at, value (number), note?, category? (finance: `groceries`, `transport`, …) |

Built-in templates for v1: habit check-in (`count`), weight (`kg`), sleep
(`h`), workout (`min`), mood (1–5), expense (currency), income (currency).

## Agent tools

| Tool | Action class |
|---|---|
| `trackers.list` / `trackers.stats` (sum, average, streak over a period) | `read` |
| `trackers.create` / `entries.log` / `entries.update` | `write-internal` |
| `trackers.delete` / `entries.delete` | `destructive` |

What these tools return for `health` and `finance` trackers depends on the
user's exposure level ([ADR 0007](../../adr/0007-sensitive-data-protection.md)):
individual entries at `full` (the default in the user's own chat), only sums,
averages and streaks at `aggregates` (the default in scheduled workflows).

## Acceptance criteria (v1)

- Logging from chat in natural language works in every supported language
  ("spent 23.40 on groceries", "спала 7 годин", "потратил 500 на такси").
- Habit streaks and simple period stats (week and month totals and averages) are
  available on every surface.
- Health and finance entries are private to their owner and cannot be shared in
  v1.
