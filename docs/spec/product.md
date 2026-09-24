# TITAN product specification

Status: **Draft v1**. This is the source of truth for what TITAN does. Client
repositories ([titan-android](https://github.com/vlukyanets/titan-android),
titan-web) link here instead of duplicating it.

## Vision

TITAN is a self-hosted AI assistant that keeps track of everything a household
or a small team cares about: tasks, time, notes, habits, health and money. It
plans your days with you, reminds you at the right moment and remembers what
you told it. It runs on your own machines, joined in a private Tailscale
network, and its agent is built on the Claude Agent SDK.

## Users and roles

| Role | Can do |
|---|---|
| **Owner** | Everything a member can, plus manage users, devices, cluster nodes, Claude credentials, budgets and default policies |
| **Member** | Use the assistant, own private data, share items with other members, set their own autonomy overrides |

- Accounts are local to TITAN (username + password; passkeys later).
- Each client device is paired once and receives its own revocable **device
  token**. API calls authenticate with the device token.
- All data belongs to exactly one user and is **private by default**. Sharing
  is explicit, per item or per collection (for example a shared "Home" project).

## Surfaces (v1)

| Surface | Repository | Purpose |
|---|---|---|
| Android app | titan-android | Primary daily client: chat, today view, domain lists, approvals, notifications |
| CLI (`titan`) | titan | Power use and administration from any node |
| Web UI | titan-web (planned) | Dashboard and chat in a browser on the tailnet |

All surfaces use the same HTTP API ([API contract](../api/README.md)). The API is
reachable **only over Tailscale**. Nothing is exposed to the public internet.

## Domains (v1, thin)

v1 covers every domain at a basic level, then deepens them. Each domain has its
own spec:

| Domain | Spec | Summary |
|---|---|---|
| Tasks and projects | [domains/tasks.md](domains/tasks.md) | Todos with due dates, priorities, recurrence, projects |
| Calendar and time planning | [domains/calendar.md](domains/calendar.md) | Events, time blocks, agent-assisted day planning and rescheduling |
| Notes, knowledge and memory | [domains/notes-memory.md](domains/notes-memory.md) | Notes, long-term agent memory, semantic search |
| Trackers | [domains/trackers.md](domains/trackers.md) | Habits, health metrics, finance entries |
| Reminders | [domains/reminders.md](domains/reminders.md) | Time-based reminders delivered as push notifications |

External integrations (Google Calendar, CalDAV, email and so on) are **out of
scope for v1**. TITAN is the source of truth for its own data.

## The assistant

- Every surface offers a **chat** with the agent. Replies stream token by token
  and show tool activity and pending approvals live.
- The agent reads and writes domain data only through domain tools. It never
  touches the database directly.
- **Scheduled workflows** run without a chat: a daily plan in the morning, a
  weekly review, and replanning when a time block is missed.
- The agent keeps **long-term memory** per user: facts, preferences and routines
  it learns, stored with embeddings for semantic recall.

## Languages

- Users write and speak **Russian, English and Ukrainian**, and often mix them
  within one message or note.
- The assistant answers in the language the user wrote in. Scheduled messages
  (daily plan, reminders) use each user's preferred language, set in their
  profile.
- Search and memory must find a note regardless of the language of the query:
  a Russian question has to find an English or mixed-language note. The
  embedding model is chosen for this (M0).

## Autonomy policy

Every domain tool declares an **action class**. What the agent may do for each
class is set by a per-domain policy:

| Action class | Examples | Default policy |
|---|---|---|
| `read` | List tasks, search notes | Auto |
| `write-internal` | Create a task, move a time block, log a habit | Auto, recorded in the audit log, can be undone |
| `external` | Anything that leaves TITAN or affects another user's private data | Ask the user for confirmation |
| `destructive` | Delete a project, purge history | Ask the user for confirmation |

- Users can override the policy per domain and per action class. Possible values
  are `auto`, `auto-undo`, `confirm` and `deny`. Example: finance `write-internal`
  = `confirm`.
- Confirmation requests appear in every surface and as a push notification. An
  unanswered request expires (configurable, default 24 h) and counts as a
  rejection.
- Every action the agent takes is written to an append-only **audit log** with
  before and after state. `auto-undo` actions can be reverted from the log.

Design: [ADR 0005](../adr/0005-per-domain-autonomy-policy.md).

## Cost control

- **Tiered models**: each workflow step is configured with a model tier (a fast,
  cheap model for classification and parsing; a strong model for planning and
  conversation).
- **Prompt caching** for system prompts and stable domain context.
- **Monthly budget per user**, tracked from token usage. At 80 % the user is
  warned. At 100 % scheduled workflows stop and chat falls back to the cheap tier
  until the owner raises the cap or the month rolls over.

## Non-goals for v1

- Public or multi-tenant hosting.
- External calendar, email or bank integrations.
- Offline-capable clients (clients are online-only; see the client specs).
- iOS or desktop native apps.
- Voice interface.
