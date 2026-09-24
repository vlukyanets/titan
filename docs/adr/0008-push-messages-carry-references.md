# 0008. Push messages carry only references

- Status: Accepted
- Date: 2026-09-24

## Context

Notifications reach phones through UnifiedPush, with an ntfy server on the
tailnet as the push server ([notifications](../spec/domains/notifications.md),
[titan-android ADR 0003](https://github.com/vlukyanets/titan-android/blob/master/docs/adr/0003-unifiedpush-notifications.md)). Two questions follow.

1. **What goes into a push message.** ntfy stores messages in its cache and runs
   on one node. Reminder texts, approval details and budget warnings are user
   content, some of it from sensitive domains
   ([ADR 0007](0007-sensitive-data-protection.md)). UnifiedPush can carry Web
   Push encrypted payloads (RFC 8291), but that needs key exchange at
   registration and encryption code on the server, and the content would still
   have to fit into 4 KB.
2. **Where the server sends requests.** The device tells the server its
   endpoint URL, and the server then makes HTTP requests to it. Taken as is,
   that lets any paired device point the server at internal addresses.

## Options

- **Full content in plaintext.** Simplest, and the notification shows even if
  the API is unreachable. But the content sits in ntfy's cache, readable by
  anyone on the tailnet who learns the topic.
- **Full content with Web Push encryption.** Private and self-contained, but
  needs key handling on both sides and still hits the size limit.
- **Only a reference.** The message holds the notification id and kind, and the
  app fetches the notification over the API. Nothing sensitive leaves the
  database. It costs one API request per push, and the push server has to be
  reachable anyway for the push to arrive, so the API usually is too.

## Decision

A push message is a JSON object with the notification id and its kind, and
nothing else:

```json
{"notification_id": "0199d6a4-...", "kind": "reminder"}
```

The app fetches `GET /api/v1/notifications/{id}` and builds the system
notification from the answer. The server accepts push endpoints only on the push
servers listed in `TITAN_PUSH_ALLOWED_ORIGINS`, does not follow redirects, and
treats endpoints as secrets: they are never logged or returned by the API.

## Consequences

- The push path needs no encryption, and ntfy never sees user content.
- A notification shows only after the API answers. When the phone reaches ntfy
  but not the API, the app can show a generic "New notification" and retry.
- Adding a distributor on another push server means adding its origin to the
  configuration.
- If a distributor ever requires Web Push encryption, the reference payload
  can be encrypted without changing its shape.
