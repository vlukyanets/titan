# Claude authentication

Decision: [ADR 0003](../adr/0003-claude-auth-modes.md).

TITAN talks to Claude through the Claude Agent SDK. The Python SDK runs a
bundled Claude Code binary as a subprocess, so the binary's authentication rules
apply.

## Modes

Set by `TITAN_CLAUDE_AUTH_MODE`:

| Mode | Credential | Intended use |
|---|---|---|
| `api-key` (default) | `ANTHROPIC_API_KEY` | Any deployment. Billed per token on the Claude Platform |
| `oauth` | `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) | The owner's personal use of their own Claude subscription. Opt-in |

### Policy note on `oauth`

The Agent SDK documentation says:

> Unless previously approved, Anthropic does not allow third party developers to
> offer claude.ai login or rate limits for their products, including agents
> built on the Claude Agent SDK. Use the API key authentication methods
> described in the Quickstart instead.
>
> https://code.claude.com/docs/en/agent-sdk/overview

`oauth` mode is therefore provided only for the owner's own use, at the owner's
own risk. It may stop working without notice. Serving other family members from
one person's subscription is not supported. When `oauth` mode is enabled,
TITAN logs a warning at startup that links to this page.

## The `oauth` mode must not fall back to an API key

Claude Code chooses the first credential it finds in this order: cloud provider
settings, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `apiKeyHelper`, then
`CLAUDE_CODE_OAUTH_TOKEN`. An API key left in the environment would win quietly
and the owner would be billed per token. In `oauth` mode TITAN therefore:

1. **Builds a clean environment** for the SDK subprocess. It removes
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
   `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
   `CLAUDE_CODE_USE_FOUNDRY` and any other provider variables. Removing them
   from the process environment before the SDK starts is required because the
   SDK merges `ClaudeAgentOptions.env` over the parent environment
   (`{**os.environ, …, **options.env}` in `subprocess_cli.py`, checked against
   `claude-agent-sdk` 0.2.159). Overriding a variable is not the same as
   removing it.
2. **Isolates configuration** with a dedicated `CLAUDE_CONFIG_DIR` inside the
   container and `setting_sources=[]`, so no user or project `settings.json`
   can add an `apiKeyHelper` or other credential.
3. **Refuses to start** if `CLAUDE_CODE_OAUTH_TOKEN` is missing, if any of the
   removed variables is still visible to the subprocess, or if a startup
   self-check fails. The self-check runs one minimal session and reads the
   `apiKeySource` field of its `system/init` message. Any value that names an
   API key source fails the check. The exact value expected for OAuth is
   pinned during the M1 spike.

The same rules work the other way round: in `api-key` mode
`CLAUDE_CODE_OAUTH_TOKEN` is removed from the subprocess environment.

## Tests

Both directions are covered by unit tests that start with a polluted environment
(for example both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` set) and
assert on the exact environment passed to the subprocess.
