# Domain: notifications

Status: **Draft v1**. Part of the [product spec](../product.md). Reminders,
approvals, the daily plan and budget warnings all reach users through this
domain.

## Entities

| Entity | Key fields |
|---|---|
| `Notification` | id, user, kind (`reminder`, `approval`, `plan`, `budget`, `system`), title, body, data, created_at, delivered_at?, read_at? |
| `PushSubscription` | device, UnifiedPush endpoint, created_at, updated_at |

- `data` is a small JSON object for the client, for example the id of the
  approval or reminder that the notification's actions refer to.
- A device has at most one push subscription. Registering again replaces the
  endpoint.

## Delivery

- Every notification is stored first, then pushed. A client that was offline
  lists what it missed from the history.
- Push goes through **UnifiedPush**. The cluster runs an **ntfy** server on the
  tailnet, and the ntfy app on the phone is the distributor, so no Google
  services are involved. After pairing, the app registers the endpoint its
  distributor gave it.
- The server pushes to every registered device of the user, except revoked
  devices and devices of disabled users. `delivered_at` is set when at least one
  push server accepted the message.
- A push message carries only the notification's id and kind. The client fetches
  the notification over the API before it shows anything
  ([ADR 0008](../../adr/0008-push-messages-carry-references.md)).
- The server accepts only endpoints on the push servers listed in its
  configuration (`TITAN_PUSH_ALLOWED_ORIGINS`), so a device cannot make the
  server send requests to arbitrary addresses.
- A push server that answers `404` or `410` has forgotten the endpoint. The
  server then deletes the subscription, and the app registers again when its
  distributor hands it a new endpoint.
- A failed push is not retried yet. The notification stays in the history.
  Retries move to the worker when it exists.

## API

| Call | Purpose |
|---|---|
| `PUT /api/v1/devices/current/push` | Register or replace the calling device's endpoint |
| `DELETE /api/v1/devices/current/push` | Stop pushes to the calling device |
| `GET /api/v1/notifications` | The caller's notifications, newest first; `unread`, `before` and `limit` |
| `GET /api/v1/notifications/{id}` | One notification, fetched by the app when a push arrives |
| `POST /api/v1/notifications/{id}/read` | Mark one as read |
| `POST /api/v1/notifications/read-all` | Mark all as read |
| `POST /api/v1/notifications/test` | Send a test notification to yourself, for the push setup screen |

A user sees only their own notifications. Someone else's notification id
answers `404`.

On a node, `titan notifications send <username> <title>` sends a `system`
notification.

## Acceptance criteria (v1)

- A test notification reaches a paired phone through ntfy within a few seconds.
- A push message contains no titles, bodies or other user content.
- Registering an endpoint on a push server that is not configured fails with
  `422`.
- A revoked device receives no further pushes.
- A notification created while the phone was offline appears in the history
  afterwards, unread.
