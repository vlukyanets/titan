# 0006. Replicated database with vector support

- Status: **Proposed**, recommendation ready (research tracked in `docs/roadmap/research/`)
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

## Evidence from the M0 spike

All four candidates ran the same failure scenarios on a three-node Docker
cluster (2026-09-24). The measurements that decided the recommendation:

- **Majority requirement.** With two of three nodes stopped, the surviving
  CockroachDB and YugabyteDB nodes could neither read nor write. pgEdge Spock
  and cr-sqlite kept working on any remaining node.
- **Jobs under partition.** YugabyteDB fired 200 of 200 jobs exactly once,
  CockroachDB fired one twice, pgEdge fired 100 twice, and cr-sqlite needed a
  home-made majority lease to avoid duplicates.
- **Concurrent edits.** pgEdge keeps the later commit and silently drops the
  other. cr-sqlite resolves per column by edit count. The Raft databases
  serialise the edits.
- **Vectors.** pgvector HNSW and CockroachDB's built-in index are stable.
  YugabyteDB's index is early access with poor, unstable recall. sqlite-vec
  has ANN search only in an alpha release.
- **Footprint.** Idle memory per node was 50–70 MB (cr-sqlite), 208–305 MiB
  (pgEdge), and 420–560 MiB (YugabyteDB, CockroachDB).
- **Health.** cr-sqlite has had no release since January 2024. Spock has been
  under the PostgreSQL License since 5.0 and is maintained by one company.

## Decision

**Recommended, awaiting the owner's acceptance:** PostgreSQL + pgEdge Spock +
pgvector, on every node.

Because replication is asynchronous, the application must be designed for it:

1. **Scheduled jobs fire at least once and are delivered idempotently.** Each
   run has a deterministic id (job id + scheduled time). Notifications carry it
   and clients drop duplicates. Workflows write results under deterministic
   keys, so a job that runs on two nodes updates one row instead of creating
   two. Each job has a preferred node, and other nodes take over only after the
   lease has expired plus a grace period.
2. **Concurrent edits are recoverable.** Updates change only the fields that
   were edited, every agent write is in the audit log
   ([ADR 0005](0005-per-domain-autonomy-policy.md)), Spock conflicts are
   surfaced to the owner, and node clocks are kept in sync.
3. **We own the database image**: PostgreSQL + Spock + pgvector built and
   pinned by us, with `spock_output` allowed in `output_plugin_libraries`.

## Consequences

- Development and single-node deployments use the same image without
  subscriptions, so M1 does not wait for the cluster work.
- Every scheduled workflow and notification needs an idempotency key. This
  becomes a rule in the architecture docs once the ADR is accepted.
- Still to verify before M3: vector recall on real data with the chosen
  embedding model, Spock's conflict log, and how much WAL the other nodes keep
  while the laptop is away for a week.
- Rejected: CockroachDB and YugabyteDB (they need a majority, which the owner's
  topology often lacks, and are heavy on a laptop); cr-sqlite (inactive
  project, alpha-only ANN search, schema changes don't replicate).
