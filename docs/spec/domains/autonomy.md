# Domain: autonomy (policy, approvals, audit log)

Status: **Draft v1**. Part of the [product spec](../product.md#autonomy-policy).
Decides what the agent may do on its own, asks the user about the rest, and
records everything it did so it can be undone.
Design: [ADR 0005](../../adr/0005-per-domain-autonomy-policy.md) and
[ADR 0010](../../adr/0010-approved-calls-run-outside-the-session.md).

## Entities

| Entity | Key fields |
|---|---|
| `PolicyRule` | user? (none for a household default), domain, action class, decision |
| `Approval` | id, user, thread?, tool, domain, action class, input, summary, status, created_at, expires_at, decided_at?, result? |
| `AuditEntry` | id, user, approval?, tool, domain, action class, decision, summary, entity, before?, after?, created_at, undone_at? |

- Action classes: `read`, `write-internal`, `external`, `destructive`.
  Decisions: `auto`, `auto-undo`, `confirm`, `deny`.
- Domains with agent tools: `accounts`, `chat`, `notifications`; the other
  domains join as they get tools. Policy rules can already be set for all of
  them.

## Policy

- The decision for a tool call is the first match of: the user's own rule for
  the domain and action class, the household default the owner set, the
  built-in default (`read` = `auto`, `write-internal` = `auto-undo`,
  `external` = `confirm`, `destructive` = `confirm`).
- Every user sets rules for themselves. Only the owner sets household defaults.
- The policy is enforced only in the Agent SDK `PreToolUse` hook. A tool the
  registry does not know is denied.
- `auto` and `auto-undo` let the call run. `deny` refuses it, and the agent is
  told that the user's policy does not allow it. `confirm` refuses the call
  and creates an approval request instead.

## Approvals

- An approval request stores the exact tool input and a one-line summary for
  people, such as "Send Boris a notification: Buy milk". It is shown in the
  chat stream as an `approval` event and sent as an `approval` notification
  whose data holds the approval id.
- The agent is told that the user was asked and that the action runs once they
  approve. The turn then ends normally.
- On approval TITAN runs the stored call itself, without the model, so what
  runs is exactly what the user saw. The result becomes an assistant message in
  the thread the request came from, and the approval records it.
- Status: `pending`, then `approved` while the call runs, then `executed` or
  `failed`; or `rejected`; or `expired`. A request that is still pending after
  `TITAN_APPROVAL_TTL_HOURS` (24 by default) is expired and counts as a
  rejection.
- A decided or expired request cannot be decided again (`409`). Only its user
  can see or decide it; for everyone else it does not exist (`404`).

## Audit log

- Every call the agent makes to a tool that is not `read` is recorded when it
  runs, with the decision that allowed it (or the approval), the summary, the
  entity it touched, and its state before and after. Entries are never edited,
  except that undoing one sets `undone_at`.
- A tool that is `write-internal` must be able to undo its change; tests fail
  for one that cannot. Undo restores the before state only if the entity still
  has the after state; otherwise it answers `409` and changes nothing.
- External calls, such as a notification sent to someone else, cannot be
  undone.
- The log holds tool input and entity state, so it is private to its user like
  the data itself.

## API

| Call | Purpose |
|---|---|
| `GET /api/v1/policy` | The caller's effective decision for every domain and action class, with where it comes from |
| `PUT /api/v1/policy/{domain}/{action_class}` | Set the caller's own rule |
| `DELETE /api/v1/policy/{domain}/{action_class}` | Remove the caller's own rule |
| `PUT /api/v1/policy/household/{domain}/{action_class}` | Set a household default (owner only) |
| `DELETE /api/v1/policy/household/{domain}/{action_class}` | Remove a household default (owner only) |
| `GET /api/v1/approvals` | The caller's approval requests, newest first; `pending` filter |
| `GET /api/v1/approvals/{id}` | One request, fetched by the app when an `approval` push arrives |
| `POST /api/v1/approvals/{id}/approve` | Approve and run it; answers with the result |
| `POST /api/v1/approvals/{id}/reject` | Reject it |
| `GET /api/v1/audit` | The caller's audit log, newest first |
| `POST /api/v1/audit/{id}/undo` | Undo one entry |

## Agent tools in M1

| Tool | Domain | Action class | Undo |
|---|---|---|---|
| `list_members` | accounts | `read` | – |
| `rename_thread` | chat | `write-internal` | Restores the old title |
| `notify_member` | notifications | `external` | – |

`notify_member` sends a `system` notification to another household member, so
the default policy asks before it runs.

## Acceptance criteria (v1)

- Asking the agent to notify another member creates an approval request that
  shows in the chat stream and arrives as a push. Approving it sends the
  notification and posts the result in the thread; rejecting it sends nothing.
- A request left alone for 24 hours can no longer be approved.
- Renaming a thread through the agent runs without asking, appears in the audit
  log, and can be undone; undoing after the title changed again answers `409`.
- A rule of `deny` for a domain stops the agent from using its tools.
