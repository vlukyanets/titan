# Accounts and devices

Status: **Draft v1**. Part of the [product spec](product.md#users-and-roles).

## Entities

| Entity | Key fields |
|---|---|
| `User` | id, username, display_name, role (`owner`, `member`), password hash, failed_logins, locked_until?, created_at, disabled_at? |
| `Device` | id, user_id, name, platform (`android`, `web`, `cli`, `other`), token hash, created_at, last_seen_at?, revoked_at? |

- Usernames are 3–32 characters: lowercase letters, digits, `.`, `_`, `-`.
  They are unique across the household.
- Passwords are at least 12 characters and are hashed with Argon2id.
- A device token is shown to the client exactly once, when the device is
  paired. The server stores only its SHA-256 hash.

## Flows

**First owner.** On a new installation the owner account is created on a node
with `titan users create <username> --owner`, which asks for the password.
There is no self-registration.

**Members.** The owner creates member accounts, with the CLI or
`POST /api/v1/users`, and tells the member their initial password.

**Pairing.** A client sends username, password, device name and platform to
`POST /api/v1/devices/pair`. On success the server creates a device and returns
its id and token. The client stores the token in secure storage and never
stores the password.

**Authenticated calls** send `Authorization: Bearer <device token>`. A missing,
unknown or revoked token, or a disabled user, gets `401`.

**Browser sign-in** (proposed in
[ADR 0012](../adr/0012-browser-sessions-for-the-web-ui.md)). The Web UI does
not pair: its sign-in form sends username and password to
`POST /api/v1/session`, which creates a `web` device and puts its token in an
`HttpOnly` cookie that page scripts cannot read. Signing out revokes the
device. A `web` device unused for 30 days expires.

**Revoking.** A user lists and revokes their own devices. The owner can revoke
any device. A revoked token fails on the next request.

## Rules

- After 10 failed passwords in a row an account is locked for 15 minutes.
  Pairing for an unknown username takes as long as for a wrong password, and
  both answer with the same error, so usernames cannot be probed.
- `last_seen_at` is updated at most once per hour per device, so ordinary
  requests do not turn into replicated writes
  ([ADR 0006](../adr/0006-replicated-database-with-vectors.md)).
- Ownership is checked in the service layer: a member cannot see or revoke
  another member's devices.

## Acceptance criteria (v1)

- The owner can be created from the CLI and can create members.
- A client can pair with username and password and then call the API with its
  device token.
- Revoking a device makes its token fail immediately on every node that has
  received the change.
- Wrong passwords lock the account as described and never reveal whether a
  username exists.
