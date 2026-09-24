# 0006. Replicated database with vector support

- Status: **Proposed** (research tracked in `docs/roadmap/research/`)
- Date: 2026-09-24

## Context

TITAN runs on peer nodes (home server, work laptop, optional VPS) connected
over Tailscale. Every node should serve reads and writes. The laptop is often
asleep or away. The same database must also store embedding vectors for
semantic search of notes and memory, with vector support either built in or
provided by an extension.

## Requirements

1. Accepts writes on more than one node, or fails over automatically without
   manual promotion.
2. Keeps working on the remaining nodes when the laptop is offline, and the
   laptop catches up when it returns.
3. Vector tables and approximate nearest-neighbour search, built in or through
   an extension.
4. Supports what the scheduler needs to fire each reminder exactly once: either
   strongly consistent transactions, or a lease design that is safe under the
   engine's conflict rules.
5. Runs in Docker on a laptop without hurting daily use.
6. Mature enough for personal data, with working backup and restore.
7. Schema migrations managed with **Alembic** on SQLAlchemy
   ([database-migrations.md](../architecture/database-migrations.md)). An
   engine that cannot be driven by Alembic needs a strong reason to win.

## Options under evaluation

| Option | Multi-writer | Laptop offline | Vectors | Alembic | Notes |
|---|---|---|---|---|---|
| PostgreSQL + pgEdge Spock + pgvector | Yes, async, conflict resolution | Tolerated | pgvector | Yes. DDL must be replicated through Spock, and the procedure needs checking | Full Postgres; per-table conflict rules to design |
| SQLite + cr-sqlite (CRDT) + sqlite-vec | Yes, merges | Built for it | sqlite-vec | Partly: batch mode for `ALTER`, plus custom operations around `crsql_begin_alter` / `crsql_commit_alter` | Very light. Leases for exactly-once are hard with CRDTs |
| CockroachDB | Yes, Raft | Needs 2 of 3 nodes up | Built-in vector type | Yes, through the `sqlalchemy-cockroachdb` dialect | Heavier; quorum means the laptop cannot be one of only two nodes |
| YugabyteDB (YSQL) + pgvector | Yes, Raft | Needs a majority | pgvector-compatible | Yes, through the Postgres dialect | Postgres-compatible; heavier memory footprint |

## Decision

Pending. It will be decided after the M0 research spike.

## Consequences

The storage layer is written behind a repository interface so the M1 skeleton
can start on plain PostgreSQL + pgvector on one node while this ADR is open.
