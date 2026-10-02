# Milestones

Last updated: 2026-10-02. Client milestones live in the client repositories and
are aligned with these.

## M0: Research and spikes (done)

Goal: settle the decisions that block the platform.

- [x] Database replication research and spike, then decide
      [ADR 0006](../adr/0006-replicated-database-with-vectors.md): PostgreSQL +
      pgEdge Spock + pgvector.
- [x] LangGraph + Agent SDK spike: folded into M1. The `chat_turn` graph runs
      Agent SDK sessions with the Postgres checkpointer and token accounting,
      and approved calls run outside the session
      ([ADR 0010](../adr/0010-approved-calls-run-outside-the-session.md)).
- [x] Claude auth spike: folded into M1. The expected `apiKeySource` values are
      pinned in [claude-auth.md](../architecture/claude-auth.md).
- [x] Embedding model: moved to M2 with the notes item, which is the first to
      need it.
- [x] Draft [ADR 0007](../adr/0007-sensitive-data-protection.md) options with a
      recommendation.

Exit: ADR 0006 accepted, the spike code thrown away or folded into M1.

## M1: Core platform (done)

- [x] `uv` project skeleton, lint (ruff), type checks (mypy), pytest,
      GitHub Actions CI.
- [x] Docker images for `titan-api` and `titan-worker`, plus a Compose file for
      one node. The `embeddings` image moved to M2 with the model choice.
- [x] SQLAlchemy + Alembic setup with CI migration tests
      ([database-migrations.md](../architecture/database-migrations.md)).
- [x] Accounts, device pairing and device tokens.
- [x] Agent runtime: LangGraph + Agent SDK node wrapper, model tiers, auth modes.
- [x] Policy hook, approvals API, audit log with undo.
- [x] Chat API with SSE streaming ([chat](../spec/domains/chat.md)).
- [x] ntfy notifier and the notification store.
- [x] OpenAPI export to `docs/api/openapi.json` with a CI drift check.
- [x] Token usage tracking per user.

Exit: an Android or CLI client can pair, chat with streaming, and approve a
confirm-class action. Met on 2026-10-02 with the CLI against a live node in
`oauth` mode.

## M2: Thin domains

- [x] Tasks and projects ([plan](plans/m2-tasks-projects.md)).
- [x] Calendar and planner (daily plan, replanning) ([plan](plans/m2-calendar.md)).
- [ ] Notes, memory and semantic search, with the embedding model and the
      `embeddings` image ([plan](plans/m2-notes.md)).
- [ ] Trackers (habits, health, finance templates) ([plan](plans/m2-trackers.md)).
- [x] Reminders with exactly-once firing ([plan](plans/m2-reminders.md)).

Exit: every acceptance criterion in `docs/spec/domains/` passes on one node.

## M3: Surfaces and cluster

- [ ] `titan` CLI covering chat, domains and admin
      ([plan](plans/m3-cli-client.md)).
- [ ] Web UI in [titan-web](https://github.com/vlukyanets/titan-web): the
      node serves its pinned build next to the API
      ([plan](plans/m3-web-ui-serving.md)), and browsers sign in with
      cookie sessions ([ADR 0012](../adr/0012-browser-sessions-for-the-web-ui.md)).
- [ ] Spike Tailscale Services on a test tailnet: failover time, the service
      certificate, draining from a container, SSE through the service
      ([ADR 0013](../adr/0013-one-cluster-address.md)).
- [ ] Multi-node deployment over Tailscale with pgEdge Spock (ADR 0006),
      behind the cluster address, with readiness-driven advertising.
- [ ] Verify a week-long laptop absence (WAL retention) and Spock conflict
      logging.
- [ ] Rolling upgrades with expand/contract migrations.

Exit: three nodes run, the laptop goes offline and comes back without data
loss, and a reminder fires exactly once.

## M4: Hardening

- [x] Monthly budget caps with warnings and fallback to the fast model tier
      ([plan](plans/m4-budget-caps.md)).
- [ ] Prompt caching tuned per workflow.
- [ ] Backups and a tested restore.
- [ ] ADR 0007 implemented.
- [ ] Weekly review workflow.
