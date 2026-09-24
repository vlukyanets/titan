# Research: replicated database with vectors

Feeds [ADR 0006](../../../adr/0006-replicated-database-with-vectors.md). Delete this
file once the ADR is accepted.

## Questions to answer per candidate

1. How are writes on two nodes at once resolved? Is the rule per table or per
   column?
2. What happens when the laptop is away for a week? Does it catch up by itself,
   and how much disk does the backlog use?
3. Can the scheduler fire a reminder exactly once? Test: two workers race for
   the same lease on two nodes during a network partition.
4. Vector search: which index types, with what recall and latency at 100 k
   chunks of 768–1024 dimensions?
5. Alembic: can a revision with a new column and a new vector index be applied
   on one node and reach the others? What manual steps are needed?
6. Memory and CPU at idle on a laptop.
7. Backup and point-in-time restore.
8. Licence (it must be usable for free, self-hosted).

## Candidates

- PostgreSQL 17 + pgEdge Spock + pgvector
- SQLite + cr-sqlite + sqlite-vec
- CockroachDB (built-in vector type)
- YugabyteDB YSQL + pgvector

## Spike

All candidates ran the same scenarios on a three-node Docker cluster, as
described in [spike/SPEC.md](spike/SPEC.md). Each candidate's folder has the
code, `RESULTS.md` and `results.json`:
[pgedge](spike/pgedge/RESULTS.md) ·
[crsqlite](spike/crsqlite/RESULTS.md) ·
[cockroach](spike/cockroach/RESULTS.md) (stopped early: no backup or licence
check) ·
[yugabyte](spike/yugabyte/RESULTS.md).

All four ran on one shared 4-core container at the same time, so timings are
indicative. Vector recall was measured on random vectors, which is a worst case
for every ANN index; it is only comparable between candidates, not with real
data.

## Findings

| | pgEdge Spock + pgvector | cr-sqlite + sqlite-vec | CockroachDB | YugabyteDB |
|---|---|---|---|---|
| Model | Async multi-master | CRDT, our own sync | Raft consensus | Raft consensus |
| S1 conflicting writes | Later commit wins, other value lost silently | Per column, higher edit count wins (not time) | Serialised, loser retries, nothing lost silently | Last writer wins in read committed |
| S2 one node offline | Writes fine, catch-up 1.9 s | Writes fine, catch-up 3.7 s | Writes fine, catch-up 2 s | Writes fine, catch-up 7.2 s |
| S2b two nodes offline | Keeps working (async) | Keeps working | **Survivor cannot read or write** | **Survivor cannot read or write** |
| S3 jobs, healthy | 200/200 with settle-and-verify lease | 200/200 | 200/200 | 200/200 |
| S3 jobs, partition | **100 fired twice** | 99 twice (0 with our own majority lease) | 1 fired twice | 200/200 |
| S4 vectors | pgvector HNSW, recall 0.79 at ef_search 400 | ANN only in an alpha, 0.65 at best | Built-in, 0.96 at beam 256, slow build | Early access, 0.14–0.34, unstable |
| S5 Alembic | Unchanged; Spock replicates DDL | Must run on every node | Works via dialect | Works; DDL not rolled back on failure |
| S6 idle memory per node | 208–305 MiB | 50–70 MB | 470–560 MiB | 420–510 MiB |
| Image | 220 MB, self-built | 246 MB, self-built | 193 MB | 3.5 GB |
| Licence | PostgreSQL License (Spock 5.x) | MIT | Proprietary, free tier with conditions (not verified) | Apache-2.0 |
| Health | One vendor, regular releases | **No cr-sqlite release since Jan 2024** | Mature | Mature |

### What decides it

The owner's cluster is a home server that is usually on, a work laptop that
often sleeps or travels, and an optional VPS. Often only one or two nodes are
up.

- **CockroachDB and YugabyteDB** need a majority of nodes. With two of three
  nodes down the survivor served nothing (S2b). With only two nodes, one
  sleeping laptop stops the whole cluster. Both also need about half a gigabyte
  of memory per idle node. This rules out requirement 2 for this topology.
- **cr-sqlite** fits the topology best on paper, but the project is inactive,
  ANN search exists only in an alpha, schema changes do not replicate, and we
  would own the sync layer.
- **pgEdge Spock** keeps working with any subset of nodes, replicates schema
  changes, has a stable vector index and a moderate footprint. Its weak spots
  are the two things async replication cannot give: silent last-update-wins
  on concurrent edits of the same row, and cluster-wide exactly-once job
  firing.

### Recommendation

**PostgreSQL + pgEdge Spock + pgvector**, with the application designed around
its two weak spots:

1. **Jobs fire at least once and are delivered idempotently.** Each job run gets
   a deterministic id (job id + scheduled time). Notifications carry it and
   clients drop duplicates. Workflows write results under deterministic keys
   (for example a UUIDv5 of user and date for the daily plan), so two nodes that
   both run the job write the same row instead of two rows. Each job has a
   preferred node. Other nodes only take over after the lease has expired plus
   a grace period, so duplicates only occur during real partitions.
2. **Concurrent edits are recoverable, not silent.** Updates change only the
   fields the user changed, every agent write is already in the audit log
   (ADR 0005), and Spock's conflict log is surfaced to the owner. Nodes must
   keep their clocks synchronised, because the resolution uses commit
   timestamps.
3. **Operations**: we build and pin our own Postgres + Spock + pgvector image,
   allow `spock_output` in `output_plugin_libraries`, and tune connection
   timeouts so replication resumes faster than the 56 s seen after a partition.

Still to check before relying on it: real-data vector recall (with the
embedding model from M0 task 4), Spock conflict logging, and a week-long
laptop absence (WAL retention on the other nodes).
