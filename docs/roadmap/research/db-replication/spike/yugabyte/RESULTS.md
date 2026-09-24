# YugabyteDB (YSQL) + pgvector spike results

Versions: `yugabytedb/yugabyte:2026.1.2.0-b137` (PostgreSQL 15.12-YB-2026.1.2.0, v2026.1 STS series),
extension `vector` 0.8.0-yb-1.0 with the `ybhnsw` access method. Harness: Python 3.11, SQLAlchemy 2.0.54,
Alembic 1.20.0, psycopg 3.3.6, numpy 2.4.6.
Run date: 2026-09-24. Host: shared 4-core container, so timings are indicative.

## Summary

YugabyteDB meets the functional requirements. Every node takes reads and writes, Raft
keeps data strongly consistent, `FOR UPDATE SKIP LOCKED` leases fired all 200 jobs
exactly once even with a partition, the schema is driven by stock Alembic through the
PostgreSQL dialect, and `ysql_dump` restored every row. It needs a majority to be up: with two of three nodes
down the survivor served nothing at all, not even `select 1` or a follower read. That
rules out the "only two nodes, laptop asleep" setup. Two things surprised me. First, with
`yugabyted --join`, each tserver kept the master list it had when it joined, so the first
partition test took down both remaining nodes until every node was restarted once. Second,
`ybhnsw` recall@10 was very low (0.19 at the default `ef_search`) once the index had settled,
and the feature is marked Early Access. Idle memory is about 420–510 MiB per node, even with
tight limits.

## Cluster setup

- Three `yugabyted start --background=false` containers. `node1` bootstraps. `node2` and
  `node3` use `--join=node1-cluster`. Each node uses `--advertise_address=nodeN-cluster`,
  an alias that exists only on the internal `cluster` network. RPC (7100/9100) and web
  (7000/9000) therefore bind to the cluster interface only, while YSQL is bound to
  `0.0.0.0:5433` (`pgsql_proxy_bind_address`) and published on 16301–16303 through
  `client`. `docker network disconnect spike-yugabyte_cluster <node>` therefore
  really partitions a node.
- Replication factor is 3: `get_universe_config` shows `numReplicas: 3`, with three masters
  and three tservers alive. Before starting, the harness waits until the master
  health check reports no under-replicated tablets.
- The default isolation set by yugabyted is **read committed**
  (`yb_enable_read_committed_isolation=true`). Wait queues are on.
- Time to YSQL ready on all three nodes: 20.5 s. Time until fully replicated: 133 s after `up`.

### Manual step required: tserver master lists (surprise)

In the first S3b run, node1 was the master leader. After it was partitioned, node3's
master became leader. The tserver logs then showed that the node1 tserver used
`[node1-cluster:7100]` and the node2 tserver used `[node2-cluster:7100,node1-cluster:7100]`,
which were the masters that existed when each node joined. yugabyted's 60 s poller rewrote
the gflag and `yugabyted.conf` (`yb-ts-cli set_flag tserver_master_addrs … --force`),
but the running client kept its old list. **YSQL on both remaining nodes failed for the
whole 420 s run, including after the partition healed.** Only 106 of 200 jobs fired, with no
duplicates. The harness now works around this: after bootstrap it waits until
`yugabyted.conf` lists all three masters, then restarts every node whose tserver was
started with fewer (node1 and node2; this takes 11.8 s). After that, partitioning the
master-leader node1 made writes on node2 fail for about 12 s before they worked again.
After the heal, node1's postgres restarted and accepted writes again after about 20 s.

## Scenarios

### S1: conflicting writes

One task was inserted on node1 and was visible on all three nodes at once, because reads
go to the Raft leader. Two threads, released by one barrier, then updated `title` on
node1 and node2 (`version = version + 1`). Main run:

- **Read committed** (default, autocommit), 5 trials: both writes succeeded every time.
  The client saw no error and did no retry. The second writer waited in the wait queue and
  then applied on top of the first, so `version` ended at 3. The last committed value won:
  node1's value in 4 trials and node2's in 1. All three nodes returned the same row
  0.18–0.31 s after both writers returned. That time is how long the harness took to poll,
  not a replication lag.
- **Repeatable read and serializable**, explicit `BEGIN; SELECT version; UPDATE; COMMIT`,
  3 trials each: in the main run the two transactions did not overlap and neither saw an
  error. In an earlier development run with the same code, one writer got
  `SerializationFailure` (40001, "could not serialize access due to concurrent update")
  in 3 of 3 repeatable-read trials, and `DeadlockDetected` (40P01) in 3 of 3 serializable
  trials. Each needed one client retry.
- The nodes cannot diverge. Every trial converged to one value.

### S2: laptop offline and catch-up

`node3` was stopped with `docker compose stop`. 10 000 single-row autocommit inserts,
alternating between node1 and node2, took 38.0 s:

- Write errors: **0**. p50 **1.34 ms**, p95 **4.6 ms**, p99 25.2 ms, max 613 ms. A
  development run showed one write stalling for 15.0 s straight after the stop, while
  tablet leaders on node3 failed over.
- Writes on node1 and node2 worked the whole time node3 was down.
- After `start node3`: YSQL on node3 was ready in 4.9 s. The node3 replica of the `tasks`
  tablet caught up in **7.2 s**. Before the start, the leader page showed node3's
  `last_applied` at 65 against 10 065 on the others; at 7.2 s node3 also showed 10 065.
  The checksum through node3 (count plus md5 of sorted `id:title`) matched after 7.3 s:
  10 011 rows, `e342885e…`. A follower read on node3 (`yb_read_from_followers`, 2 s
  staleness) returned the same checksum.

### S2b: two of three nodes stopped

This result comes from a follow-up run on a fresh cluster (setup, S5, then S2b). In the
main run the harness crashed at this step because `compose start` blocked on
`depends_on` health checks. It now uses `docker start`.

- With node2 and node3 stopped, **node1 served nothing.** The first insert hung for 29 s
  and ended with `AdminShutdown: terminating connection due to administrator command`.
  After that, postgres on node1 was restarting ("the database system is shutting down",
  "server closed the connection unexpectedly"). `count(*)`, `select 1` and a follower
  read all failed.
- After node2 was started again (2 of 3 up), **writes on node1 still timed out for the
  whole 300 s window** (38 statement timeouts of 5 s each). I did not find the cause in
  the time-box. The master lists on all nodes were complete afterwards. S2, S3b and the
  partition probe all showed that two healthy nodes do serve writes.
- Once node3 was also started, node1 accepted writes straight away and the cluster was
  fully replicated 0.3 s later.

### S3: exactly-once jobs

Claim statement, run in autocommit (read committed) with a 30 s lease:

```sql
UPDATE jobs SET lease_owner = :w, lease_until = now() + interval '30 seconds'
WHERE id = (SELECT id FROM jobs
            WHERE done_at IS NULL AND run_at <= now()
              AND (lease_until IS NULL OR lease_until < now())
            ORDER BY run_at LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING id
```

The worker then fsyncs `<id>,<worker>` to a local file and marks the job done with
`UPDATE … SET done_at = now() WHERE id = :id AND lease_owner = :w AND done_at IS NULL`.
Retries on 40001/40P01 are counted.

| Run | Exactly once | More than once | Never | Retries | Errors seen by workers |
|---|---|---|---|---|---|
| (a) healthy | 200 (w1 80, w2 120) | 0 | 0 | 0 | none. Took 4.0 s |
| (b) node1 partitioned at 100 fired, 60 s, then healed | 200 (w1 56, w2 144) | 0 | 0 | 0 | w1: 67 "server closed the connection unexpectedly", 6 "database system is shutting down", 1 `Rpc timeout`. w2: 1 statement timeout (10 s) during failover |

In (b), worker w1 on the partitioned node could not claim anything. w2 finished the
remaining 100 jobs within the 60 s partition. After the heal the cluster was fully
replicated within 0.2 s. `SKIP LOCKED` is supported on YSQL, and no serialization
retries were needed.

### S4: vector search

- Revision 0002 created `ybhnsw (vec vector_cosine_ops) WITH (m=16, ef_construction=64)`
  on the empty table through plain `op.create_index(postgresql_using="ybhnsw")`.
  `USING hnsw` would also be accepted and rewritten to `ybhnsw`.
- Inserting 20 000 unit vectors (dim 384, `default_rng(42)`) with `COPY` on node1, with the
  index present, took 3.8 s. Building the same index on the populated 20k-row table took
  **17.0 s**, with `CREATE INDEX NONCONCURRENTLY`, which takes an ACCESS EXCLUSIVE lock.
- node2 saw 20 000 rows, and `EXPLAIN` showed `Index Scan using embeddings_vec_ybhnsw`
  with no extra steps.
- 100 top-10 queries (`default_rng(7)`) on node2:

| ef_search | p50 | p95 | recall@10 |
|---|---|---|---|
| 40 (default) | 2.04 ms | 14.35 ms | **0.191** (min 0.0) |
| 100 | 1.95 ms | 3.03 ms | 0.342 |
| exact seq scan (10 queries) | 102 ms | – | 1.0 |

- Follow-up on a fresh cluster with the same data and the first 30 queries: straight after
  loading, recall was 0.883 at ef 40 and 0.99 at ef 400 (exact scan 1.0, so the ground truth
  is correct). A few minutes later it had settled at **0.143 (ef 40) and 0.36 (ef 100)**
  on both nodes, and it stayed there over three repeats. The index returns rows in
  distance order. Re-ranking the top 200 by distance gives only 0.547. I suspect the drop
  comes from background flush or compaction of the Vector LSM (`vector_index_backend=yb_hnsw_hnswlib`),
  but did not confirm it. The pgvector page marks the feature **Early Access**.

### S5: Alembic across the cluster

- `alembic upgrade 0001_initial` on node1 took 4.0 s. It ran `CREATE EXTENSION vector`
  and created three tables. All three tables and `alembic_version = 0001_initial` were
  visible on node2 and node3 at the first check, because the catalog is shared through
  the masters.
- With node3 stopped, `alembic upgrade head` on node1 **succeeded but took 31.5 s**,
  compared with about 1 s on a healthy cluster. node3 was master leader when it was
  stopped. In a development run it took 17 s. After `start node3`, node3's YSQL was ready
  in 2.7 s and at once showed `priority`, `embeddings_vec_ybhnsw` (am `ybhnsw`, `m=16,
  ef_construction=64`) and `alembic_version = 0002_priority_and_ann_index`. No manual
  steps were needed.
- **Incompatibility:** Alembic assumes transactional DDL, but YSQL does not provide it by
  default (`ysql_yb_ddl_transaction_block_enabled=false`). A `CREATE TABLE` and a
  `CREATE INDEX` inside `BEGIN … ROLLBACK` both **survived the rollback**, while the
  inserted row was rolled back. A migration that fails halfway therefore leaves
  partial schema with the old `alembic_version`. `CREATE INDEX` inside a transaction
  becomes non-concurrent ("Create index in transaction block cannot be concurrent").
- Extra check on a single node with the Early Access flag
  `ysql_yb_ddl_transaction_block_enabled=true`: the DDL was rolled back correctly, and
  `alembic upgrade head` succeeded in 3.1 s.
- Other points: `ybhnsw` builds block writes on the table (no concurrent build).
  `ADD COLUMN … DEFAULT` on vector columns is not supported (per the docs; not tested).

### S6: footprint

Configured limits: master `memory_limit_hard_bytes` 384 MiB, tserver 768 MiB,
block caches of 32 MiB (master) and 64 MiB (tserver), `ysql_num_shards_per_tserver=1`,
`--ui=false`, and a Docker `mem_limit` of 2 GiB. Measurements were taken after 60 s idle with data loaded:

| Node | docker stats memory | idle CPU (3 samples) | data on disk | processes (RSS) |
|---|---|---|---|---|
| node1 | 507 MiB | 4.4–6.7 % | 145 MB | tserver 304, postgres×5 273, master 101, yugabyted (python) 52 MiB |
| node2 | 467 MiB | 5.0–5.3 % | 131 MB | tserver 273, postgres×5 276, master 89, python 52 MiB |
| node3 | 420 MiB | 4.3–13.3 % | 132 MB | tserver 257, postgres×5 275, master 68, python 52 MiB |

Image: 855 MB compressed content (`docker image inspect .Size` = 854 889 119 bytes).
`docker images` reports 3.48 GB on disk. The idle nodes each drew about 5 % CPU.

### S7: backup and restore

- The supported methods are `ysql_dump`/`ysqlsh` for logical export and import,
  distributed snapshots (`yb-admin create_database_snapshot`) and point-in-time recovery.
  `yugabyted backup` requires YB Controller.
- `ysql_dump` of node1 took 5.4 s (75 MB). A restore into a fresh single-node yugabyted
  took 19.1 s with 0 errors. **Row counts matched**: tasks 10 011, jobs 200, embeddings
  20 000, alembic_version 1. The `ybhnsw` index and `alembic_version` were restored too.
- `yb-admin create_database_snapshot ysql.yugabyte` completed (state `COMPLETE`). Restoring
  from the snapshot was not tested.

### S8: licence and project health

| Component | Licence | Latest release | Conditions |
|---|---|---|---|
| YugabyteDB core (DB, YSQL, yugabyted, tools) | Apache 2.0 | 2026.1.2.0-b137, 2026-09-22 (v2026.1 STS: EOM 2027-06-29, EOL 2027-12-29). Latest LTS: 2025.2.6.0 | None for self-hosting. YugabyteDB Anywhere is a separate product under the Polyform Free Trial licence (32 days), which the spike does not use |
| pgvector (bundled 0.8.0-yb-1.0) | PostgreSQL licence | ships with the DB | Feature is Early Access in YugabyteDB |
| psycopg 3 | LGPL-3.0 | 3.3.6 (as installed) | – |
| SQLAlchemy / Alembic | MIT | 2.0.54 / 1.20.0 (as installed) | – |

Sources:
- https://github.com/yugabyte/yugabyte-db/blob/master/LICENSE.md
- https://github.com/yugabyte/yugabyte-db/blob/master/docs/content/stable/releases/ybdb-releases/v2026.1.md
- https://github.com/yugabyte/yugabyte-db/blob/master/docs/data/currentVersions.json
- https://github.com/yugabyte/yugabyte-db/blob/master/src/postgres/third-party-extensions/pgvector/LICENSE
- https://github.com/yugabyte/yugabyte-db/blob/master/docs/content/stable/additional-features/pg-extensions/extension-pgvector.md (ybhnsw, EA tag, limitations)
- https://github.com/yugabyte/yugabyte-db/blob/master/docs/content/stable/explore/transactions/transactional-ddl.md
- https://github.com/yugabyte/yugabyte-db/blob/master/docs/content/stable/deploy/checklist.md (minimum 2 cores and 2 GB RAM)
- https://github.com/psycopg/psycopg/blob/master/LICENSE.txt, https://github.com/sqlalchemy/sqlalchemy/blob/main/LICENSE, https://github.com/sqlalchemy/alembic/blob/main/LICENSE
- Image tags: https://hub.docker.com/r/yugabytedb/yugabyte/tags

docs.yugabyte.com was blocked by the egress proxy, so the docs were read from their
source in the GitHub repository.

## Adaptations and manual steps

- The image runs as uid 10001 and cannot write to a fresh named volume, so data lives in
  the container's writable layer (`--base_dir=/home/yugabyte/ybd`). It survives
  stop/start and is removed by `down -v`.
- `pgsql_proxy_bind_address=0.0.0.0:5433` is needed so YSQL is reachable on `client`,
  while cluster RPC stays on the `cluster` alias. The `cluster` network is `internal: true`.
- **Every node except the last to join must be restarted once after bootstrap**, so
  that its tserver learns all three masters. See "Manual step required" above.
- Reconnecting after a partition needs `docker network connect --alias nodeN-cluster`.
- The schema uses `uuid`, `timestamptz` and `vector(384)` unchanged. The ANN index is
  `ybhnsw` with `vector_cosine_ops`. Alembic runs unmodified with
  `postgresql+psycopg`, but DDL is not transactional unless the EA flag is on.
- Memory settings: `memory_limit_hard_bytes` (master 384 MiB, tserver 768 MiB),
  `db_block_cache_size_bytes`, one shard per tserver, UI off, callhome off.
- Time-box: the coordinator asked me to finish early. The main run used the full sizes
  (10 000 rows, 20 000 vectors, 100 queries, 60 s partition). S2b and the S4 recall
  follow-up come from a second, shorter session, as described above.

## Verdict against ADR 0006 requirements 1–7

| Requirement | Met? | Evidence |
|---|---|---|
| 1. Writes on more than one node, or automatic failover | Yes | Every node accepts writes (S1, S2, S3). Raft failover after partitioning the master leader took about 12 s of write errors on the other nodes, once the master-list restart had been done |
| 2. Keeps working with the laptop offline; laptop catches up | Partly | With 1 of 3 down: 0 write errors, and node3 caught up 10k rows in 7.2 s (S2). With 2 of 3 down: no reads or writes at all, and no recovery within 300 s after the second node returned (S2b). The laptop cannot be one of only two nodes |
| 3. Vectors and ANN search | Partly | `vector` plus `ybhnsw` work across nodes with no extra steps, and queries take about 2 ms. Recall@10 was only 0.19 at the default ef (0.14–0.36 after the index settled), and the feature is Early Access |
| 4. Exactly-once scheduler support | Yes | `FOR UPDATE SKIP LOCKED` lease: 200/200 exactly once, healthy and partitioned. Strongly consistent transactions |
| 5. Runs in Docker on a laptop without hurting daily use | Doubtful | About 420–510 MiB and about 5 % CPU per idle node with tight limits. The image is 3.5 GB on disk. The docs' minimum is 2 GB RAM per node |
| 6. Mature, with working backup and restore | Mostly | Core is Apache-2.0, with regular releases (latest 2026-09-22). `ysql_dump` round trip matched all counts, and snapshots work. The vector feature and transactional DDL are Early Access |
| 7. Alembic | Yes, with a caveat | Stock Alembic with the PostgreSQL dialect worked, including with a node down. DDL is not rolled back on failure unless the EA flag `ysql_yb_ddl_transaction_block_enabled` is set. Vector index builds lock the table |
