# Plan: M1 policy hook, approvals and audit log

Implements the "Policy hook, approvals API, audit log with undo" item of
[M1](../milestones.md): the [autonomy spec](../../spec/domains/autonomy.md),
[ADR 0005](../../adr/0005-per-domain-autonomy-policy.md) and
[ADR 0010](../../adr/0010-approved-calls-run-outside-the-session.md).

Branch `feature/m1-policy-approvals`, stacked on `feature/m1-chat-turn`.

## Design

- `titan.domains.autonomy`: policy rules, approval requests and audit
  entries, with their services. The action classes and decisions are domain
  enums. `ToolSpec` (name, domain, action class, JSON schema, summary, run,
  undo) lives here too, so each domain declares its tools in its own
  `tools.py` without importing the agent runtime.
- `titan.agent.tools`: the registry of every domain's tools, the in-process
  SDK MCP server per domain, and the wrapper that runs a tool and writes its
  audit entry. `execute()` is the one path that runs a tool, used by the model
  (through MCP) and by an approval.
- `titan.agent.policy`: the `PreToolUse` hook. It resolves the decision,
  denies unknown tools, and for `confirm` stores the request, sends the
  `approval` notification and emits the `approval` stream event before denying
  the call.
- Domain tools are not put in `allowed_tools`, so a call that the hook does
  not allow falls back to Claude Code's own permission check and is refused.
- Expiry is checked when a request is read or decided; there is no scheduler
  yet.

Tasks:

- [x] Spec, ADR 0010 and this plan.
- [x] Autonomy tables, services and migration: policy resolution, approval
      states and expiry, audit entries and undo with conflict detection.
- [x] Tool layer, policy hook and the first tools (`list_members`,
      `rename_thread`, `notify_member`), wired into `chat_turn`, with a fake
      Claude Code that calls hooks and tools the way the CLI does.
- [ ] Policy, approvals and audit API, OpenAPI regenerated, docs updated.
- [ ] Live: the M1 exit scenario with a real credential (blocked).
