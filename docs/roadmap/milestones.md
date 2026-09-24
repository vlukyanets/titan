# Milestones

Last updated: 2026-09-24. Client milestones live in the client repositories and
are aligned with these.

## M0: Research and spikes

Goal: settle the decisions that block the platform.

- [x] Database replication research and spike, then decide
      [ADR 0006](../adr/0006-replicated-database-with-vectors.md): PostgreSQL +
      pgEdge Spock + pgvector.
- [ ] LangGraph + Agent SDK spike: `daily_plan` end to end with one domain tool,
      a policy interrupt, resume on another process, and token accounting
      ([ADR 0002](../adr/0002-langgraph-with-agent-sdk-nodes.md)).
- [ ] Claude auth spike: confirm env sanitisation and pin the `apiKeySource`
      value expected in `oauth` mode ([claude-auth.md](../architecture/claude-auth.md)).
- [ ] Pick the embedding model and container image (multilingual, CPU-friendly).
- [x] Decide [ADR 0007](../adr/0007-sensitive-data-protection.md): disk
      encryption, a `sensitive` replication set, aggregates for scheduled
      workflows.

Exit: ADR 0006 accepted, the spike code thrown away or folded into M1.

## M1: Core platform

- [ ] `uv` project skeleton, lint (ruff), type checks (mypy or pyright), pytest,
      GitHub Actions CI.
- [ ] Docker images for `titan-api`, `titan-worker` and `embeddings`, plus a
      Compose file for one node.
- [ ] SQLAlchemy + Alembic setup with CI migration tests
      ([database-migrations.md](../architecture/database-migrations.md)).
- [ ] Accounts, device pairing and device tokens.
- [ ] Agent runtime: LangGraph + Agent SDK node wrapper, model tiers, auth modes.
- [ ] Policy hook, approvals API, audit log with undo.
- [ ] Chat API with SSE streaming.
- [ ] ntfy notifier and the notification store.
- [ ] OpenAPI export to `docs/api/openapi.json` with a CI drift check.
- [ ] Token usage tracking per user.

Exit: an Android or CLI client can pair, chat with streaming, and approve a
confirm-class action.

## M2: Thin domains

- [ ] Tasks and projects.
- [ ] Calendar and planner (daily plan, replanning).
- [ ] Notes, memory and semantic search.
- [ ] Trackers (habits, health, finance templates).
- [ ] Reminders with exactly-once firing.

Exit: every acceptance criterion in `docs/spec/domains/` passes on one node.

## M3: Surfaces and cluster

- [ ] `titan` CLI covering chat, domains and admin.
- [ ] Web UI in `titan-web` (the repository still has to be created).
- [ ] Multi-node deployment over Tailscale with pgEdge Spock (ADR 0006).
- [ ] Verify a week-long laptop absence (WAL retention) and Spock conflict
      logging.
- [ ] Rolling upgrades with expand/contract migrations.

Exit: three nodes run, the laptop goes offline and comes back without data
loss, and a reminder fires exactly once.

## M4: Hardening

- [ ] Monthly budget caps with warnings and fallback to the fast model tier.
- [ ] Prompt caching tuned per workflow.
- [ ] Backups and a tested restore.
- [ ] ADR 0007 implemented.
- [ ] Weekly review workflow.
