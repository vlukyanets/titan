# Domain: trackers (habits, health, finance)

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

Trackers are structured time series. They share one data model, so adding a new
kind of tracker needs no new code.

## Entities

| Entity | Key fields |
|---|---|
| `Tracker` | id, owner, name, kind (`habit`, `health`, `finance`, `custom`), unit (`count`, `kg`, `min`, `EUR`, …), min_value?, max_value?, target? (for example "8 glasses per day"), schedule (RRULE)?, archived |
| `Entry` | id, tracker_id, at, value (number), note?, category? (finance: `groceries`, `transport`, …) |

Built-in templates for v1: habit check-in (`count`), weight (`kg`), sleep
(`h`), workout (`min`), mood (1–5), expense (currency), income (currency).

## Trackers

- Every tracker belongs to one user and only they see it. Sharing is not in
  v1 for any kind; for `health` and `finance` it stays that way.
- A name is 1–100 characters and unique among the owner's trackers, ignoring
  case, so "log my weight" names one tracker. A unit is 1–16 characters.
- A template fills in the kind, the unit and the bounds; the name always comes
  from the caller, in the user's language. Any template field can be
  overridden. Expense and income have no default currency, so they need a
  unit.

  | Template | Kind | Unit | Bounds |
  |---|---|---|---|
  | `habit` | `habit` | `count` | ≥ 0 |
  | `weight` | `health` | `kg` | ≥ 0 |
  | `sleep` | `health` | `h` | 0–24 |
  | `workout` | `health` | `min` | ≥ 0 |
  | `mood` | `health` | `score` | 1–5 |
  | `expense` | `finance` | given | ≥ 0 |
  | `income` | `finance` | given | ≥ 0 |

- `min_value` and `max_value` bound new entries. Changing them later does not
  touch existing entries.
- A target is a value, a period (`day`, `week`, `month`) and a direction:
  `at_least` (8 glasses a day) or `at_most` (a spending cap of 300 a week).
- A schedule is an RRULE with the same rules as
  [task recurrence](tasks.md#recurrence). It names the days a habit is due, so
  it needs a daily target or no target.
- An archived tracker keeps its entries, is left out of the default list and
  takes no new entries until it is restored. Deleting a tracker deletes its
  entries.

## Entries

- `at` is when it happened, by default now; entries can be back-dated.
- Values are exact decimals with at most four places (more are rounded), so
  money adds up to the cent.
- A note is at most 1000 characters. A category is 1–32 characters, stored in
  lower case.
- Entries are listed newest first by `at`.

## Periods, stats and streaks

- A day is a local calendar day in the owner's time zone
  ([calendar](calendar.md#time-zones)), a week runs Monday to Sunday and a
  month is a calendar month.
- Stats cover whole periods between two local dates: per period and in total,
  the number of entries, their sum, average, minimum and maximum; the total
  also has the sum per period, and the sum per category. Periods without
  entries are included, so charts have no gaps. One request covers at most
  400 periods.
- A period **meets** a tracker's target when the sum of its entries reaches it
  (`at_least`) or stays within it (`at_most`). Without a target, a period
  meets it when it has at least one entry.
- The **streak** is the number of met periods in a row up to now. The current
  period counts once it is met, and does not break the streak while it is not
  met yet. The target's period is the unit, a day without a target. With a
  schedule only due days count; the other days neither add to the streak nor
  break it. Periods before the tracker was created do not count, so a new
  spending cap does not start with a streak of years. A streak counts back at
  most 1000 periods.

## API

| Call | Purpose |
|---|---|
| `GET /api/v1/trackers/templates` | The built-in templates |
| `GET /api/v1/trackers?kind=&archived=` | The caller's trackers, by name; archived ones only when asked for |
| `POST /api/v1/trackers` | Create a tracker, optionally from a `template` |
| `GET`, `PATCH`, `DELETE /api/v1/trackers/{id}` | Read, change, delete with its entries |
| `GET /api/v1/trackers/{id}/entries` | Entries, newest first; filters: `from`, `to` (times), `category`; paged with `before` and `limit` |
| `POST /api/v1/trackers/{id}/entries` | Log an entry |
| `PATCH`, `DELETE /api/v1/trackers/{id}/entries/{entry_id}` | Change or delete an entry |
| `GET /api/v1/trackers/{id}/stats?period=&from=&to=` | Stats and the streak; `from` and `to` are local dates, by default the last 30 days, 12 weeks or 12 months |

`PATCH` changes only the fields it sends; sending `null` clears an optional
field. Another user's tracker answers `404`.

## Agent tools

| Tool | Action class |
|---|---|
| `trackers.list` / `trackers.stats` (sum, average, streak over a period) | `read` |
| `trackers.create` / `entries.log` / `entries.update` | `write-internal` |
| `trackers.delete` / `entries.delete` | `destructive` |

The default policy for `finance` and `health` trackers may be stricter once
[ADR 0007](../../adr/0007-sensitive-data-protection.md) is decided.

## Acceptance criteria (v1)

- Logging from chat in natural language works ("spent 23.40 on groceries",
  "slept 7 hours").
- Habit streaks and simple period stats (week and month totals and averages) are
  available on every surface.
- Health and finance entries are private to their owner and cannot be shared in
  v1.
