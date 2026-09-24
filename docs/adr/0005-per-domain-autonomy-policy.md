# 0005. Per-domain autonomy policy

- Status: Accepted
- Date: 2026-09-24

## Context

The agent acts on personal and family data. Some actions are harmless and easy
to undo, such as moving a time block. Others affect other people or cannot be
taken back. A single global switch ("suggest only" or "fully autonomous") is
either too noisy or too risky, and users trust different domains differently.

## Decision

- Every domain tool declares one **action class**: `read`, `write-internal`,
  `external` or `destructive`. An `external` action leaves TITAN or affects
  another user's data.
- A **policy** maps (user, domain, action class) to one of `auto`, `auto-undo`,
  `confirm` or `deny`.
- Defaults: `read` = `auto`, `write-internal` = `auto-undo`, `external` =
  `confirm`, `destructive` = `confirm`. The owner sets household defaults.
  Members override them for their own data.
- The policy is enforced in one place: an Agent SDK `PreToolUse` hook. Tools do
  not check the policy themselves.
- `auto-undo` and `confirm` actions are written to an append-only audit log with
  before and after state. Undo applies the inverse change if the entity has not
  changed since. Otherwise it reports a conflict.

## Consequences

- Adding a tool means declaring its action class. Tests fail for tools without
  one.
- Approval requests need their own API, notifications and UI in every client.
- Undo needs every write-internal tool to return enough state to revert it.
