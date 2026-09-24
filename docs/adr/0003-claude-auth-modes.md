# 0003. Claude authentication modes

- Status: Accepted
- Date: 2026-09-24

## Context

The owner has a Claude subscription and would like to use it instead of paying
per token. The Agent SDK runs the Claude Code binary, which accepts
`CLAUDE_CODE_OAUTH_TOKEN`. The Agent SDK documentation does not allow third
party products to offer claude.ai login unless Anthropic has approved it, and
the Claude API itself documents only API keys and other non-subscription
methods. Details and the quote are in
[claude-auth.md](../architecture/claude-auth.md).

## Decision

TITAN supports two modes, chosen by `TITAN_CLAUDE_AUTH_MODE`:

- `api-key` (default): `ANTHROPIC_API_KEY`.
- `oauth` (opt-in, owner's personal use): `CLAUDE_CODE_OAUTH_TOKEN`.

The selected mode is strict. The SDK subprocess environment is cleaned so that
no credential from the other mode, or from a cloud provider, can take
precedence. Configuration is isolated, and a startup self-check refuses to run
if the active credential source is not the selected one.

## Consequences

- Switching modes is a configuration change and a restart. No code changes.
- `oauth` mode may break if Anthropic restricts subscription tokens further.
  `api-key` mode is always the supported fallback.
- Budget tracking (tokens per user) works the same in both modes, because it
  counts tokens rather than money.
