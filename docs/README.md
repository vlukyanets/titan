# TITAN documentation

This folder holds the **permanent** documentation for the TITAN backend. It
describes how the system is meant to work and why. Planning material that
changes often lives separately in [`roadmap/`](roadmap/README.md).

## Specification: what TITAN does

- [Product spec](spec/product.md): vision, users, surfaces, autonomy, cost control
- [CLI](spec/cli.md): node administration and the client commands
- Domains:
  [tasks](spec/domains/tasks.md) ·
  [calendar](spec/domains/calendar.md) ·
  [notes and memory](spec/domains/notes-memory.md) ·
  [trackers](spec/domains/trackers.md) ·
  [reminders](spec/domains/reminders.md) ·
  [notifications](spec/domains/notifications.md) ·
  [chat](spec/domains/chat.md) ·
  [autonomy](spec/domains/autonomy.md) ·
  [usage](spec/domains/usage.md)

## Architecture: how it is built

- [Overview](architecture/overview.md)
- [Claude authentication](architecture/claude-auth.md)
- [Database migrations](architecture/database-migrations.md)
- [API contract](api/README.md)

## Decisions: why it is built this way

| ADR | Title | Status |
|---|---|---|
| [0001](adr/0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](adr/0002-langgraph-with-agent-sdk-nodes.md) | LangGraph workflows with Claude Agent SDK nodes | Accepted |
| [0003](adr/0003-claude-auth-modes.md) | Claude authentication modes | Accepted |
| [0004](adr/0004-openapi-from-fastapi.md) | OpenAPI schema generated from FastAPI | Accepted |
| [0005](adr/0005-per-domain-autonomy-policy.md) | Per-domain autonomy policy | Accepted |
| [0006](adr/0006-replicated-database-with-vectors.md) | Replicated database with vector support | Proposed |
| [0007](adr/0007-sensitive-data-protection.md) | Protection of sensitive domains | Proposed |
| [0008](adr/0008-push-messages-carry-references.md) | Push messages carry only references | Accepted |
| [0009](adr/0009-chat-history-in-titan-tables.md) | Chat history lives in TITAN tables | Accepted |
| [0010](adr/0010-approved-calls-run-outside-the-session.md) | Approved tool calls run outside the model session | Accepted |
| [0011](adr/0011-cli-client-over-http.md) | The CLI client talks HTTP and keeps its token in a private file | Accepted |

New ADRs start from the [template](adr/0000-template.md).

## Process

- [Contributing: commits and pull requests](CONTRIBUTING.md)
- [Roadmap (volatile)](roadmap/README.md)
