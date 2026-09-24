# CockroachDB spike results

Versions: CockroachDB CCL v26.3.2 (`cockroachdb/cockroach:v26.3.2`), SQLAlchemy
2.0.54, sqlalchemy-cockroachdb 2.0.4, Alembic 1.20.0, psycopg 3.3.6.
Run date: 2026-09-24. Host: shared 4-core container, so timings are indicative.

**Incomplete.** The spike agent was stopped on the owner's request while S2b
was running. S1–S6 finished. S2b has partial observations. S7 and S8 were not
run. This file was written afterwards from `results.json` and `out/run.log`
(the log is not committed).

## Summary

CockroachDB behaves like a single strongly consistent database as long as a
majority of nodes is up. Concurrent writes never lost silently: the slower
transaction was retried (`40001`) and one value won everywhere. Jobs fired
exactly once when healthy and with one duplicate out of 200 during a
partition. Alembic works through the `sqlalchemy-cockroachdb` dialect. With
two of three nodes stopped, the surviving node could not insert, count or do
follower reads. That is the same majority problem as YugabyteDB. It is also the
heaviest candidate on disk (about 1.4 GB per node for this small data set).

## Scenarios

### S1: conflicting writes

Ten trials each in two modes. Autocommit `UPDATE`: both writes succeeded with no
error or retry, and the node whose write committed last won (8 of 10 node2, 2 of
10 node1). Explicit read-modify-write transactions: in every trial one side
got a `40001` serialization error, retried once and succeeded. All nodes showed
the same value immediately; the insert was visible everywhere after 0.03 s.

### S2: laptop offline and catch-up

node3 stopped. 10 000 inserts alternating node1 and node2: 10 000 ok, 0
errors, p50 2.68 ms, p95 4.81 ms. After node3 started: SQL ready in 1.9 s,
checksum matched through node3 as gateway in 2.0 s, local replica point read in
3.1 s. The cluster reported under-replicated ranges until 174 s after restart.

### S2b: two of three nodes stopped (partial)

With node2 and node3 stopped, every insert, `count(*)` and follower-read query
on node1 hit the 5 s statement timeout. Only a stale point read of an already
cached row and opening new connections worked. The harness then stayed in this
state until the agent was stopped, so recovery after restarting the nodes was
not measured.

### S3: exactly-once jobs

Claim with a single `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED
LIMIT 1) RETURNING`, under SERIALIZABLE.

- (a) healthy: 200 exactly once, 0 duplicates, 0 never fired, 0 retries,
  4.9 s.
- (b) node1 partitioned for 60 s after 100 jobs: 199 exactly once, **1 fired
  twice**, 0 never fired. Each worker hit one statement timeout (`57014`). The
  duplicate is a job that worker 1 fired while its lease was expiring and
  worker 2 then claimed again. The done-update from worker 1 failed
  (`lost_lease_on_done`: 1). Under-replicated ranges cleared 39 s after the
  partition healed.

### S4: vector search

Built-in `VECTOR(384)` with a `VECTOR INDEX` (vector search enabled through a
cluster setting). Inserting 20 000 vectors with the index in place took 240 s
(100-row batches). Building the index on the populated table took 260 s. The
query plan on node2 used the index without extra steps.

| `vector_search_beam_size` | recall@10 | p50 ms | p95 ms |
|---|---|---|---|
| 32 | 0.30 | 33 | 101 |
| 64 | 0.52 | 35 | 95 |
| 128 | 0.77 | 59 | 196 |
| 256 | 0.96 | 129 | 465 |
| 512 | 0.96 | 106 | 272 |

An exact scan was p50 208 ms. Random vectors are a worst case for any ANN index.

### S5: Alembic across the cluster

`upgrade 0001_initial` on node1: the schema was visible on node2 and node3
0.13 s later. With node3 stopped, `upgrade head` on node1 took 2.0 s. node3 came
back with the new column, the vector index and `alembic_version` at
`0002_priority_and_ann_index`, with no manual steps. Alembic runs with
non-transactional DDL on CockroachDB. `alembic check` reported false
differences (VARCHAR vs TEXT, the vector index expression) because the dialect
does not recognise the `vector` type.

### S6: footprint

Idle memory 470–560 MiB per node, idle CPU 8–24 % (samples taken shortly after
heavy load), data directory 1.40–1.51 GB per node for 10 000 tasks, 200 jobs
and 20 000 vectors, image 193 MB.

### S7: backup and restore

Not run (stopped).

### S8: licence and project health

Not run (stopped). Cockroach Labs moved CockroachDB to a proprietary licence
with a free tier that has conditions (revenue limit, telemetry) in late 2024.
The current terms were not verified from primary sources in this spike.

## Adaptations and manual steps

- `sqlalchemy-cockroachdb` dialect for Alembic, non-transactional DDL.
- Vector index support switched on with a cluster setting before revision
  `0002`.
- Nodes advertise cluster-network names so that disconnecting `cluster`
  really partitions a node.

## Verdict against ADR 0006 requirements

| Requirement | Met? | Evidence |
|---|---|---|
| 1 Multi-writer or automatic failover | Yes | Every node writes. Conflicts are serialised with one retry, and nothing is lost silently |
| 2 Laptop offline, remaining nodes work | Partly | Works with one of three nodes down. With two down the survivor cannot read or write (S2b) |
| 3 Vectors and ANN | Yes | Built-in type and index, recall 0.96 at beam 256 on random data, slow index build |
| 4 Exactly-once jobs | Mostly | 200/200 healthy. 1 duplicate in 200 during a partition, caused by lease expiry |
| 5 Runs on a laptop | Doubtful | About 0.5 GiB and noticeable CPU per idle node, 1.4 GB on disk for a small data set |
| 6 Mature, backup | Unknown | Mature product. Backup and licence were not checked |
| 7 Alembic | Yes | Works through the dialect, with non-transactional DDL and noisy autogenerate |
