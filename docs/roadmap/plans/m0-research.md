# Plan: M0 research and spikes

Milestone: [M0](../milestones.md#m0-research-and-spikes). Delete this plan when M0
is done.

## Goal

Settle the decisions that block the M1 platform work, using evidence from
spikes run in containers rather than vendor claims.

## Tasks

### 1. Replicated database with vectors (ADR 0006)

Branch `research/m0-db-replication`. Decided in
[ADR 0006](../../adr/0006-replicated-database-with-vectors.md). The spike code
and results were removed from the tree afterwards and remain in git history.

- [x] Shared spike specification: same schema, scenarios and result format for
      every candidate.
- [x] Spike: PostgreSQL + pgEdge Spock + pgvector.
- [x] Spike: SQLite + cr-sqlite + sqlite-vec.
- [x] Spike: CockroachDB (stopped early: backup and licence not checked).
- [x] Spike: YugabyteDB + pgvector.
- [x] Comparison table and recommendation in the research notes.
- [x] ADR 0006 updated with the recommendation. It stays Proposed until the
      owner accepts it.
- [x] Owner decision on ADR 0006: accepted.

### 2. LangGraph + Agent SDK spike (ADR 0002)

Branch `research/m0-langgraph-agent-sdk`. **Blocked**: needs a Claude
credential in the environment.

- [ ] `daily_plan` graph with one domain tool as an SDK MCP server.
- [ ] `PreToolUse` policy hook raising a LangGraph interrupt, resumed from a
      different process.
- [ ] Postgres checkpointer, token accounting per run.

### 3. Claude auth check (ADR 0003)

Branch `research/m0-claude-auth`. **Blocked** on the same credential.

- [ ] Environment cleaning in both modes, with a unit test.
- [ ] Record the `apiKeySource` value reported in `oauth` and `api-key` modes.

### 4. Embedding model

Branch `research/m0-embeddings`.

- [ ] Shortlist multilingual CPU-friendly models (English and Russian at
      least).
- [ ] Benchmark on a small bilingual note set: quality, latency, memory.
- [ ] Decide the model and container image.

### 5. Sensitive data protection (ADR 0007)

Branch `spec/m0-sensitive-data`. Depends on task 1.

- [ ] Evaluate the options against the chosen database.
- [ ] Recommendation in ADR 0007.
