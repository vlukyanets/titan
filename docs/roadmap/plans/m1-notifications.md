# Plan: M1 ntfy notifier and notification store

Implements [notifications](../../spec/domains/notifications.md) and
[ADR 0008](../../adr/0008-push-messages-carry-references.md), the "ntfy notifier
and the notification store" item of [M1](../milestones.md). Branch
`feature/m1-ntfy-notifications`, stacked on device pairing.

## Design

- `titan.domains.notifications`: models `Notification` and `PushSubscription`
  (one per device, keyed by device id), and `NotificationsService`, which owns
  its transactions and every ownership check.
- `titan.notify`: the UnifiedPush sender, an HTTP POST of the reference payload
  through one shared `httpx.AsyncClient` with a short timeout, no redirects and
  no proxy from the environment, plus endpoint origin checks. Results are
  `delivered`, `gone` (`404`/`410`, the subscription is deleted) or `failed`.
- Pushes are sent right after the notification is committed, concurrently to
  all of the user's devices. No retries until the worker exists.
- `TITAN_PUSH_ALLOWED_ORIGINS`: comma-separated origins such as
  `http://100.64.0.1:8080`. Empty means push registration is refused.
- API: push registration under `/devices/current/push`, the notification
  history under `/notifications`, and `POST /notifications/test`.
- CLI: `titan notifications send <username> <title> [--body]`.
- Compose: an `ntfy` service published on the Tailscale address, port 8080,
  with its web app disabled; the API allows that origin.
- The `str_enum` column helper moves from the accounts models to
  `titan.storage.base` so both domains use it.

## Tasks

- [x] Spec: `docs/spec/domains/notifications.md`; reminders spec points to it.
- [x] ADR 0008: push messages carry only references.
- [ ] Models and the Alembic revision.
- [ ] UnifiedPush sender and endpoint checks, with tests.
- [ ] `NotificationsService`, API router, problem mapping.
- [ ] CLI `notifications send`.
- [ ] Tests: registration, origin checks, delivery, gone endpoints, revoked
      devices, history, read state, ownership, CLI.
- [ ] ntfy in Compose, checked end to end against a real ntfy.
- [ ] OpenAPI regenerated; API docs, overview and milestones updated.
