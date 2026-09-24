# 0006. Replicated database with vector support

- Status: Accepted
- Date: 2026-09-24
- Spike code and full results: `docs/roadmap/research/db-replication/` and the
  PostgreSQL 18 follow-up `docs/roadmap/research/db-replication-pg18/` in the
  git history (removed from the tree once their findings were recorded here)

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

### Follow-up on PostgreSQL 18

Rerun after the owner fixed PostgreSQL 18 as the minimum and made all three
nodes equal peers ([ADR 0007](0007-sensitive-data-protection.md)), on
PostgreSQL 18.6 with Spock 5.0.11 and pgvector 0.8.6:

- **Field-level updates do not prevent lost edits.** During a partition one
  node ran `UPDATE … SET title` and the other `UPDATE … SET notes` on the same
  row. After healing every node had the second row image and the title change
  was gone: Spock applies whole rows, so last commit wins per row even when
  each side touched a different column. A duplicate natural key inserted on
  both sides left the nodes permanently diverged, with one entry in
  `spock.exception_log` and nothing in `spock.resolutions`.
- **Leases without a preferred node.** Two schedulers on two nodes racing for
  the same 300 reminders fired 61 of them twice on a healthy cluster and all
  300 twice during a partition.
- **Laptop away.** After a three-minute absence with 20 000 writes the laptop
  caught up in 8 s. Its peers kept up to 700 MB of WAL for it, most of it from
  an index build.
- **Index builds stall replication.** `CREATE INDEX` is replicated and each
  node builds the index inside its apply stream. An HNSW build on 100 000
  vectors of dimension 768 took 70 s on the origin, and nothing else reached
  the laptop for about 4.5 minutes.
- **Alembic and ORM.** Revisions and downgrades applied on one node reached
  the others within 0.1 s, and `alembic check` on a replica found no drift.
  SQLAlchemy async with asyncpg and with psycopg 3 passed every check: JSONB
  filters, upserts with `RETURNING`, pgvector round trips and distance
  ordering, `FOR UPDATE SKIP LOCKED`, savepoints.
- **Idle cost** was 66 MiB and 0.1 % CPU per node.
- **YugabyteDB under partition.** With one node cut off, one of the two
  remaining nodes could read and insert but failed every UPDATE with a
  catalog RPC timeout for 15 minutes, and still failed 8 minutes after the
  partition healed. This confirms the rejection.

## Decision

PostgreSQL + pgEdge Spock + pgvector, on every node: PostgreSQL 18 or newer,
Spock 5.0.x (6.0 is still beta), pgvector 0.8.x.

Because replication is asynchronous, the application must be designed for it:

1. **Scheduled jobs fire at least once and are delivered idempotently.** Each
   run has a deterministic id (job id + scheduled time). Notifications carry it
   and clients drop duplicates. Workflows write results under deterministic
   keys, so a job that runs on two nodes updates one row instead of creating
   two. Each job has a preferred node, and other nodes take over only after the
   lease has expired plus a grace period.
2. **Concurrent edits are avoided, and recoverable when they happen.** All
   API processes send writes to one preferred node (the home server) while it
   is reachable and to their local node only when it is not, so conflicting
   edits can only happen during a partition. Rows that several writers change
   at once are append-only or use Spock `delta_apply` columns. Updates still
   change only the fields that were edited, every agent write is in the audit
   log ([ADR 0005](0005-per-domain-autonomy-policy.md)), Spock conflicts and
   `spock.exception_log` entries are surfaced to the owner, and node clocks
   are kept in sync. Primary keys are UUIDs generated by the application, and
   natural-key unique constraints are kept to a minimum because they can
   diverge across a partition.
3. **We own the database image**: PostgreSQL + Spock + pgvector built and
   pinned by us, with `spock_output` allowed in `output_plugin_libraries`
   (required from PostgreSQL 17.11 and 18.6), `UTF8` databases, and the
   `spock` and `vector` extensions created on every node before it
   subscribes.

## Consequences

- Development and single-node deployments use the same image without
  subscriptions, so M1 does not wait for the cluster work.
- Every scheduled workflow and notification needs an idempotency key
  ([architecture overview](../architecture/overview.md#scheduler-and-reminders)).
- Large indexes are built in their own revision in a quiet window, because
  they pause replication to every node while they build.
- Still to verify before M3: vector recall on real data with the chosen
  embedding model, and how much WAL the other nodes keep while the laptop is
  away for a week (a `max_slot_wal_keep_size` cap plus re-adding the laptop is
  the fallback).
- Rejected: CockroachDB and YugabyteDB (they need a majority, which the owner's
  topology often lacks, and are heavy on a laptop); cr-sqlite (inactive
  project, alpha-only ANN search, schema changes don't replicate).
