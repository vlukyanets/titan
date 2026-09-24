# Plan: M1 accounts, device pairing and device tokens

Implements [accounts and devices](../../spec/accounts.md), the "Accounts,
device pairing and device tokens" item of [M1](../milestones.md). Branch
`feature/m1-device-pairing`, stacked on the project skeleton.

## Design

- `titan.domains.accounts`: SQLAlchemy models `User` and `Device`, password
  hashing (Argon2id via argon2-cffi, run in a worker thread), token generation
  and hashing, and `AccountsService`, which owns its transactions and every
  ownership check.
- Device tokens: `tt_` + 32 random bytes (URL-safe base64). Stored as a SHA-256
  hex digest with a unique index, looked up by hash.
- Failed logins: a counter on the user row; the tenth failure sets
  `locked_until` 15 minutes ahead. Locked, unknown and wrong-password attempts
  all run one Argon2 verification and return the same error.
- `last_seen_at` is written only when it is older than one hour.
- Primary keys are UUIDv7 from `titan.storage.ids` (Python 3.12 has no
  `uuid.uuid7`).
- `titan.api.accounts`: `POST /devices/pair`, `GET /me`, `GET /devices`,
  `DELETE /devices/{id}`, `GET /users`, `POST /users`, with a Bearer dependency
  that returns the caller's user and device.
- CLI: `titan users create <username> [--owner]` and `titan users list`.
- First Alembic revision: `users` and `devices`.

## Tasks

- [x] Spec: `docs/spec/accounts.md`, linked from the product spec.
- [x] UUIDv7 helper with tests.
- [x] Models and the first Alembic revision.
- [x] Passwords, tokens, validation rules, `AccountsService`.
- [x] Bearer authentication dependency and the accounts router.
- [x] CLI `users create` and `users list`.
- [x] Tests: pairing, authentication, revocation, lockout, enumeration,
      ownership, owner-only user management, CLI.
- [x] OpenAPI regenerated; API docs and milestones updated.
