# API contract

Decision: [ADR 0004](../adr/0004-openapi-from-fastapi.md).

- The backend is the source of truth. The FastAPI app generates the OpenAPI 3.1
  schema from its routers and Pydantic models.
- The schema is exported to [`openapi.json`](openapi.json) with
  `uv run titan openapi` and committed with every API change.
- A test regenerates the schema and fails if the committed file differs, so it
  can never drift from the code.
- Clients generate their code from the committed file:
  - [titan-android](https://github.com/vlukyanets/titan-android) generates its
    Kotlin client at build time.
  - titan-web (planned) generates a TypeScript client.
- Streaming endpoints (chat over SSE) are described in the schema as
  `text/event-stream`. Their event types are documented as schema components.

## Conventions

- Base path `/api/v1`. Breaking changes need a new version prefix.
- Authentication: `Authorization: Bearer <device token>`. A device gets its
  token once from `POST /api/v1/devices/pair` (username, password, device name,
  platform); a missing, unknown or revoked token answers `401` with
  `WWW-Authenticate: Bearer`. Flows and rules:
  [accounts and devices](../spec/accounts.md).
- Timestamps are RFC 3339 in UTC. Recurrence rules are RFC 5545 RRULE strings.
- Errors use RFC 9457 problem details (`application/problem+json`). Validation
  errors list the failing fields but never echo submitted values.
- `GET /api/v1/health` (process is up) and `GET /api/v1/health/ready` (database
  reachable, current Alembic revision) and `POST /api/v1/devices/pair` are the
  only unauthenticated endpoints.
  Container health checks use them.
- Push: a device registers its UnifiedPush endpoint with
  `PUT /api/v1/devices/current/push`. A push message is only
  `{"notification_id": "...", "kind": "..."}`; the client fetches
  `GET /api/v1/notifications/{id}` to show it
  ([notifications](../spec/domains/notifications.md),
  [ADR 0008](../adr/0008-push-messages-carry-references.md)).
- Chat: `POST /api/v1/chat/threads/{id}/messages` answers with
  `text/event-stream`: `turn`, then `text` and `tool` events, then `done` or
  `error`. Every event's data is JSON whose `type` repeats the event name, and
  the `done` event's message is the stored reply, which replaces the streamed
  text. Refusals (`404`, `409` while a reply is running, `422`, `503` when the
  node has no working Claude credential) are ordinary problem responses sent
  before the stream starts ([chat](../spec/domains/chat.md)). An `approval`
  event carries a request the agent made during the reply.
- Approvals: an `approval` push carries only the approval id; the app fetches
  `GET /api/v1/approvals/{id}` and answers with `POST …/approve` or
  `POST …/reject`. Approving runs exactly the stored call and answers with the
  result; a decided or expired request answers `409`. The audit log
  (`GET /api/v1/audit`, `POST /api/v1/audit/{id}/undo`) and the policy
  (`/api/v1/policy`) are described in [autonomy](../spec/domains/autonomy.md).
- Usage: `GET /api/v1/usage?month=YYYY-MM` gives the caller's token usage and
  cost for a UTC calendar month, overall and per model;
  `GET /api/v1/usage/household` gives everyone's (owner only)
  ([usage](../spec/domains/usage.md)).
- Tasks and projects: `/api/v1/tasks` and `/api/v1/projects`. `PATCH` changes
  only the fields it sends, and `null` clears an optional one. A task is
  completed only with `POST /api/v1/tasks/{id}/complete`, which answers the
  task and, for a recurring one, its next occurrence. Items shared with the
  caller behave like their own; anything else answers `404`
  ([tasks](../spec/domains/tasks.md)).
- Reminders: `/api/v1/reminders`. A fired reminder is a `reminder`
  notification whose data carries `reminder_id`; its Snooze and Done actions
  call `POST /api/v1/reminders/{id}/snooze` (optional `minutes`) and
  `POST /api/v1/reminders/{id}/dismiss`. Snoozing a recurring reminder answers
  a new one-off reminder, because the series has already moved on
  ([reminders](../spec/domains/reminders.md)).
- Calendar: `/api/v1/calendar`. `GET /calendar/events?start=&end=` answers
  the occurrences in a window of at most 92 days, with recurring events
  expanded in their own IANA time zone, so a view is one call.
  `GET /calendar/busy` gives merged busy intervals and `/calendar/prefs` the
  caller's time zone and working hours ([calendar](../spec/domains/calendar.md)).
- Trackers: `/api/v1/trackers`. Values are JSON numbers stored as exact
  decimals with four places, so sums of money are exact.
  `GET /trackers/{id}/stats?period=&from=&to=` takes local dates and answers
  every day, week or month in between in the owner's time zone, with the
  streak; `GET /trackers/templates` lists the built-in templates. Another
  user's tracker answers `404` ([trackers](../spec/domains/trackers.md)).
