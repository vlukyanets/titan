# Spike specification: replicated database with vectors

Every candidate for [ADR 0006](../../../../adr/0006-replicated-database-with-vectors.md)
runs the same scenarios on a three-node cluster in Docker, so the results can
be compared directly. This file is the contract for each candidate's spike.

## Candidates and folders

| Candidate | Folder | Compose project | Host ports |
|---|---|---|---|
| PostgreSQL + pgEdge Spock + pgvector | `pgedge/` | `spike-pgedge` | 16001–16009 |
| SQLite + cr-sqlite + sqlite-vec | `crsqlite/` | `spike-crsqlite` | 16101–16109 |
| CockroachDB | `cockroach/` | `spike-cockroach` | 16201–16209 |
| YugabyteDB (YSQL) + pgvector | `yugabyte/` | `spike-yugabyte` | 16301–16309 |

Each folder contains:

- `docker-compose.yml` with three nodes: `node1`, `node2`, `node3`.
- A Python harness managed with `uv` (`pyproject.toml`, `harness.py` or a
  small package), started by `./run.sh`, which runs every scenario from a
  clean cluster and tears it down at the end.
- `migrations/`: an Alembic environment with the two revisions below.
- `RESULTS.md`: findings in the format at the end of this file.
- `results.json`: the raw numbers.

The spike code is throwaway. It must run, but it does not need production
quality.

## Networks

Every node joins two Docker networks:

- `client`: the harness and host-published ports use it.
- `cluster`: replication traffic between nodes uses it. Peers address each
  other by names that resolve only on this network.

A **partition** of a node means disconnecting it from `cluster` with
`docker network disconnect`, so clients can still reach it. **Offline** means
`docker compose stop` (or `pause`) of the whole node, which is what a
sleeping laptop looks like.

## Schema

Alembic revision `0001_initial`:

```sql
tasks(
  id          uuid primary key,
  owner       text not null,
  title       text not null,
  status      text not null,
  updated_at  timestamp with time zone not null,
  version     integer not null default 1
)
jobs(
  id           uuid primary key,
  kind         text not null,
  run_at       timestamp with time zone not null,
  lease_owner  text,
  lease_until  timestamp with time zone,
  done_at      timestamp with time zone
)
embeddings(
  id         uuid primary key,
  entity_id  uuid not null,
  chunk      integer not null,
  vec        vector(384) not null   -- or the engine's equivalent
)
```

Alembic revision `0002_priority_and_ann_index`: add `tasks.priority integer
not null default 0` and create the engine's approximate nearest-neighbour index
on `embeddings.vec` (HNSW if available).

Adapt types where the engine requires it (for example SQLite has no `uuid`
type). Write down every adaptation.

## Scenarios

**S1: conflicting writes.** Insert one task and wait until every node has
it. Then update its `title` on `node1` and on `node2` at the same moment (two
threads, released by one barrier), with different values. Wait for
convergence. Record: did both writes succeed, did the client see an error or
retry, which value won on each node, did the nodes converge and how long did
it take.

**S2: laptop offline and catch-up.** Stop `node3`. Insert 10 000 tasks,
alternating between `node1` and `node2`, and record write errors and the p50
and p95 write latency. Start `node3` again. Record the time until `node3` has
all rows and an identical checksum (for example `md5` of sorted ids and
titles). Record whether writes on `node1` and `node2` worked at all while
`node3` was down.

**S3: exactly-once jobs.** Insert 200 jobs with `run_at = now()`. Run one
worker process against `node1` and one against `node2`. Each worker loops:
claim one due, unleased or lease-expired job with a 30-second lease, "fire" it
by appending `<job id>,<worker>` to a local file outside the database, mark it
done, and repeat until no due jobs are left. Use the best claim technique the
engine supports (`UPDATE … RETURNING` with `SKIP LOCKED`, a conditional
update with a version check, and so on) and describe it. Run it twice:

- (a) healthy cluster;
- (b) partition `node1` from `node2` and `node3` after 100 jobs have fired,
  keep both workers running for 60 seconds, then heal the partition.

Record per run: jobs fired exactly once, more than once, never; errors seen
by the workers.

**S4: vector search.** Insert 20 000 random unit vectors of dimension 384
(`numpy.random.default_rng(42)`) on `node1` after revision `0002` created the
index. On `node2`, run 100 top-10 queries (query vectors from
`default_rng(7)`) with the ANN index. Compute recall@10 against exact
brute-force results computed in Python. Record index build time, p50 and p95
query latency, recall@10, and whether the vectors and index were usable on
`node2` without extra steps.

**S5: Alembic across the cluster.** Run `alembic upgrade 0001_initial`
against `node1` only and check that the schema appears on `node2` and `node3`.
Then stop `node3`, run `alembic upgrade head` on `node1`, start `node3`, and
check that it ends up with the new column and index and with an
`alembic_version` row saying `0002_priority_and_ann_index`. Record every
manual or engine-specific step needed.

**S6: footprint.** After S1–S5, with data loaded, record per node: idle
memory (`docker stats --no-stream`), idle CPU, data size on disk, and the
image size.

**S7: backup and restore.** Record the supported backup method. If it takes
less than 15 minutes to try, back up `node1` and restore it into a fresh
single node, and record whether the row counts match.

**S8: licence and project health.** Record the licence of each component,
the latest release and its date, and any conditions for free self-hosted use.
Use primary sources (the project's own licence file, releases page or
documentation) and cite them.

## Result format (`RESULTS.md`)

```markdown
# <Candidate> spike results

Versions: <images and package versions>
Run date: <YYYY-MM-DD>. Host: shared 4-core container, so timings are indicative.

## Summary
<3–5 sentences: does it meet ADR 0006's requirements, the main surprise>

## Scenarios
### S1 … S8
<what happened, numbers, how it was done>

## Adaptations and manual steps
## Verdict against ADR 0006 requirements 1–7
| Requirement | Met? | Evidence |
```

Report only what was actually observed. If a scenario could not be run, say
why and what was tried. Never estimate a result that was not measured.
