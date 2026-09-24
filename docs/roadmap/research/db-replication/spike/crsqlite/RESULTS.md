# SQLite + cr-sqlite + sqlite-vec spike results

Versions: cr-sqlite v0.16.3 (`crsqlite-linux-x86_64.zip` from the GitHub
release, sha256-checked), sqlite-vec 0.1.10a4 (PyPI), SQLite 3.46.1, Python 3.11
(`python:3.11-slim`), Alembic 1.16.5, SQLAlchemy 2.0.43. Node image
`spike-crsqlite-node` (built locally, removed after the run).
Run date: 2026-09-24. Host: shared 4-core container, so timings are indicative.

## Summary

cr-sqlite does multi-writer replication and offline catch-up well. Every write
succeeded with node3 down, node3 caught up on 10 000 rows in 3.6 s, and
conflicts converge in about 15 ms. It covers only the data. SQLite has no
server and cr-sqlite has no network layer, so we wrote the node process, the
changeset sync, and the lease logic ourselves (node.py is 357 lines; the sync
core is about 130). DDL does not replicate. Every node has to run Alembic
itself, and a node whose schema lags stops accepting all changes from upgraded
peers until it migrates. Conflicts are settled per column, by edit count and
then by comparing values, not by time. As a result, S3 exactly-once is only
possible with a majority-vote lease that we built on top. The plain
conditional-update lease fired 99 of 200 jobs twice under partition. The
biggest surprises were that approximate nearest-neighbour search exists only in
an sqlite-vec **alpha** (DiskANN; recall@10 0.31 with the defaults on this data),
and that cr-sqlite has had no release since January 2024.

## Architecture built for the spike

- Each node is a container. A Python process (`node/node.py`, stdlib HTTP
  server) owns `/data/titan.db` with cr-sqlite and sqlite-vec loaded. There is
  one connection, serialised by a lock, in WAL mode.
- The harness sends SQL through `POST /sql` on the published ports
  16101–16103 (client network).
- Sync is a pull loop per peer: `GET http://<peer>-cluster:8080/changes?since=<wm>`
  runs a 2 s long-poll that returns rows from `crsql_changes` with
  `db_version > wm AND site_id IS NOT <requester>`, in pages of 5 000 cut at a
  `db_version` boundary. The node applies them with
  `INSERT INTO crsql_changes …` and stores the per-peer watermark (the peer's
  local `db_version`) in the same transaction. The `*-cluster` aliases resolve
  only on the `cluster` network, so `docker network disconnect` really cuts
  replication.
- Code size: node.py 357 lines, of which the sync, watermark and barrier code
  is about 130. worker.py (S3) is 167 lines.
- Things that were hard or surprising:
  - Merged rows get the receiver's own `db_version`, so watermarks are per peer.
  - Changes arrive twice, directly and relayed through the third node.
    node3 applied 60 000 change rows from each peer for 10 000 tasks. This is
    harmless because merging is idempotent, but it doubles the work.
  - A single change that cannot be applied, such as an unknown column, rolls
    back the whole batch and blocks that peer's feed.
  - Python's `http.server` needs `disable_nagle_algorithm`. Without it every
    keep-alive request took about 40 ms. The first full run was discarded for
    this reason (see `results.json` → `first_run_discarded`).

## Scenarios

### S5: Alembic across the cluster (run first; it creates the schema)

- `alembic upgrade 0001_initial` on node1 only (through the node's `/migrate`
  endpoint, which pauses sync, closes the connection, runs Alembic and reopens):
  node1 had the tables. **node2 and node3 had no tables and no
  `alembic_version`. The schema does not replicate.**
- Running the same upgrade on node2 and node3 took about 0.4 s each. After
  that a probe row replicated in 8 ms.
- We stopped node3 and ran `upgrade head` on node1 (0.39 s). Alembic logged
  "Will assume non-transactional DDL" for SQLite.
- **Mixed versions:** node1 (at 0002) wrote a task with `priority=5`. node2
  (still at 0001) did not receive it. Its sync from node1 failed with
  `OperationalError: SQL logic error` and blocked everything from node1. After
  `upgrade head` on node2, the row arrived within 0.21 s, because the watermark
  had not advanced.
- Starting node3 again: the entrypoint runs `alembic upgrade head` when the DB
  already has `alembic_version`. node3 ended with the `priority` column, the
  `vec_embeddings` DiskANN index and `alembic_version = 0002_priority_and_ann_index`.
  The priority row arrived 0.94 s after `docker compose start`.
- Batch-mode probe (`node/probe_batch_alter.py`): Alembic
  `batch_alter_table(recreate="always")` with `drop_column` on a CRR, wrapped in
  `crsql_begin_alter` / `crsql_commit_alter`, worked. The cr-sqlite triggers
  were recreated, clock values survived, and a change from the altered replica
  merged into a replica with the old schema.

### S1: conflicting writes

Two threads released by one barrier ran
`UPDATE tasks SET title=?, version=version+1` on node1 and on node2.

| Trial | node1 wrote | node2 wrote | Winner on all 3 nodes | Converged |
|---|---|---|---|---|
| simultaneous | "A title from node1" | "B title from node2" | **B title from node2** | yes, 0.015 s |
| simultaneous | "B title from node1" | "A title from node2" | **B title from node1** | yes, 0.015 s |
| node2 partitioned; node1 edits title 3×, node2 edits once 1 s later | "A node1 third edit" | "B node2 later edit" | **A node1 third edit** (version 4) | yes, 0.47 s after heal |

- Both writes always succeeded, with no error or retry (3–6 ms).
- Conflicts are resolved per column: the higher `col_version` (number of local
  edits) wins, and a tie goes to the larger value.
- The rule is not last-writer-wins by time. In trial 3 the later write lost.
- `version = version + 1` is not an optimistic lock. Two concurrent
  increments both produce 2, and the column is just another LWW cell.

### S2: laptop offline and catch-up

- node3 was stopped. We inserted 10 000 tasks, alternating node1 and node2, one
  HTTP request per row: **0 write errors, p50 2.15 ms, p95 4.30 ms,
  p99 12.8 ms**, 25.2 s wall time. Writes on node1 and node2 were unaffected.
- node1 and node2 were identical 0.06 s after the last insert.
- `docker compose start node3`: the process was up in 1.0 s. **node3 had all
  10 005 rows after 3.59 s and an identical md5 (sorted id|title) after 3.67 s.**

### S3: exactly-once jobs

200 jobs with `run_at = now()`. Worker w1 used node1 and w2 used node2. We
tried two claim techniques (`worker.py`):

- **barrier** (conditional update):
  `UPDATE jobs SET lease_owner, lease_until WHERE id=? AND done_at IS NULL AND (lease_owner IS NULL OR lease_until < now)`.
  Then `/barrier`: wait until every *reachable* peer has pulled this
  `db_version`, and pull once from each of them. Then re-read the row and fire
  only if `lease_owner` is still us.
- **vote** (majority vote on a CRR `job_votes(job_id, voter, candidate)`):
  each node casts one vote per job for the first claim it sees. The vote is
  cast by the node's post-merge hook, and a claim is the claimer's own node
  voting. A worker fires only with 2 of 3 votes. Each cell has exactly one
  writer, so merges cannot flip a vote.

| Run | exactly once | more than once | never | worker errors |
|---|---|---|---|---|
| barrier (a) healthy | 200 | 0 | 0 | 0 (w1 lost 3 claims after merge, w2 lost 1 locally) |
| barrier (b) node1 partitioned 60 s after 101 fired | 101 | **99** | 0 | 0 |
| vote (a) healthy | 200 | 0 | 0 | 0 |
| vote (b) node1 partitioned 60 s after 101 fired | 200 | **0** | 0 | 0 (w1: 6 claims timed out undecided, 1 lost) |

- Under partition the barrier lease behaves as AP. Isolated node1 sees no
  reachable peers and fires everything it can see, so both sides fired the
  remaining 99 jobs.
- The vote lease is CP. Isolated node1 could not reach a majority, and node2
  plus node3 finished the jobs.
- Weaknesses of the vote lease:
  - It needs node3 as a tie-breaker. With the laptop off and two concurrent
    claimants it can deadlock on a 1–1 split.
  - It has no ballots, so an undecided or abandoned claim is never retried.
  - Lease expiry (`lease_until`) is informational only.
- The jobs table converged on all nodes within 0.03 s after each run.

### S4: vector search

- vec0 `CREATE VIRTUAL TABLE vec_embeddings USING vec0(vec float[384] distance_metric=cosine INDEXED BY diskann(neighbor_quantizer=binary))`
  was created in revision 0002. It is local on each node, fed by triggers on
  the replicated `embeddings.vec` BLOB (see Adaptations).
- Inserting 20 000 unit vectors (rng 42) on node1 in batches of 500, with the
  DiskANN index maintained by triggers: **173 s** (index build time).
- node2 built its own index from the replicated BLOBs as they arrived. It had
  all 20 000 rows indexed **17.8 s after node1 finished**. No extra steps were
  needed on node2.
- 100 top-10 queries on node2 (rng 7), measured end to end over HTTP:

| Method | p50 | p95 | recall@10 |
|---|---|---|---|
| DiskANN, default `search_list_size` (128) | 3.17 ms | 5.18 ms | **0.314** |
| DiskANN, `search_list_size_search=512` | 13.4 ms | 19.6 ms | 0.651 |
| exact `ORDER BY vec_distance_cosine(vec, ?)` over the CRR table | 47.1 ms | 56.1 ms | 1.000 |

  Random 384-d vectors are a hard case for ANN. Stable sqlite-vec 0.1.9 has
  only brute force (vec0 without `INDEXED BY`).

### S6: footprint (idle, after S1–S5 with data loaded)

| Node | docker stats mem | Python RSS (VmRSS / peak) | idle CPU (3 samples) | /data on disk |
|---|---|---|---|---|
| node1 | 260 MiB | 49 MB / 67 MB | 0.63%, 0.42%, 0.45% | 239 MB (db 176 MB + WAL 63 MB) |
| node2 | 305 MiB | 70 MB / 92 MB | 0.20%, 0.20%, 0.14% | 260 MB (db 176 MB + WAL 84 MB) |
| node3 | 297 MiB | 69 MB / 93 MB | 0.21%, 0.21%, 0.07% | 260 MB (db 176 MB + WAL 84 MB) |

- docker's memory figure includes the page cache for the data files. The
  process itself is about 50–70 MB.
- Image: 246 MB on disk (58.7 MB content), based on `python:3.11-slim`.
- The data holds each vector twice (the CRR BLOB and the DiskANN index with
  72 binary-quantised neighbours per node), plus cr-sqlite clock tables.

### S7: backup and restore

- Method: `VACUUM INTO '/tmp/backup.db'` on the live node1 (online, consistent
  snapshot). It took 0.60 s for 175 MB. We copied the file into a fresh
  single-node container (compose service `restore`, no peers).
- Row counts matched for all tables: tasks 10 005, jobs 800, embeddings 20 000,
  job_votes 1 200, vec_embeddings 20 000. `alembic_version` was 0002, and an
  ANN query returned 10 rows.
- **The restored database keeps node1's cr-sqlite site id.** Restoring it as an
  additional replica next to the original would give two writers the same site
  id. A new node should be seeded differently, or its site id changed. We did
  not test that.

### S8: licence and project health

| Component | Licence | Latest release | Health |
|---|---|---|---|
| cr-sqlite | MIT ([LICENSE](https://github.com/vlcn-io/cr-sqlite/blob/main/LICENSE)) | v0.16.3, 2024-01-17 ([release](https://github.com/vlcn-io/cr-sqlite/releases/tag/v0.16.3)) | Commits on main: 911 (2022), 1 239 (2023), 13 (2024), 0 (2025), 5 (2026, build fixes by an outside contributor, last 2026-08-10). **No release in 2.7 years.** |
| sqlite-vec | MIT or Apache-2.0 ([MIT](https://github.com/asg017/sqlite-vec/blob/main/LICENSE-MIT), [Apache](https://github.com/asg017/sqlite-vec/blob/main/LICENSE-APACHE)) | stable v0.1.9, 2026-03-31; pre-release v0.1.10-alpha.4, 2026-05-17 ([releases](https://github.com/asg017/sqlite-vec/releases), [PyPI](https://pypi.org/project/sqlite-vec/)) | Active in 2026 (104 commits). Pre-v1, and minor versions may break (`site/versioning.md`). ANN (DiskANN, IVF) only in alphas. |
| SQLite | public domain ([copyright](https://www.sqlite.org/copyright.html)) | 3.46.1 as shipped in the base image | Mature |

- No conditions for free self-hosted use were found.
- Dates come from git tag dates (blobless clone of both repositories) and PyPI
  upload times. The GitHub API and release HTML pages returned 403 through this
  session's proxy, and sqlite.org could not be re-fetched (proxy 403).

## Adaptations and manual steps

- Types: `uuid` became TEXT, `timestamptz` became ISO-8601 TEXT, and
  `vector(384)` became a BLOB of 384 float32 values.
- cr-sqlite rules:
  - It refuses NOT NULL columns without a DEFAULT, so every NOT NULL column
    got one (`''`, `0`, `x''`, epoch).
  - Primary keys must be NOT NULL.
  - Tables become CRRs with `SELECT crsql_as_crr('t')` inside the Alembic
    revision.
  - `alembic_version` stays local and is not replicated.
- `ALTER` on a CRR is wrapped in `crsql_begin_alter` / `crsql_commit_alter`.
  Alembic runs with `render_as_batch=True`. For ADD COLUMN, batch mode emits a
  plain ALTER; a recreate-style batch also works (S5 probe).
- The Alembic env loads both extensions on connect and calls `crsql_finalize()`
  on close.
- Schema is not replicated. Every node runs Alembic on its own file: through
  `/migrate`, or at start in the entrypoint when the DB is already
  bootstrapped. Until all nodes are migrated, sync from upgraded nodes into
  older ones is blocked. The planned "one node migrates, the others wait for
  replication" flow in `database-migrations.md` does not apply. Each node must
  migrate before it can accept new-schema changes. There is no cluster-wide
  migration lock.
- A vec0 virtual table cannot be a CRR: `crsql_as_crr` on it fails with
  "SQL logic error". Vectors replicate as BLOBs in `embeddings`. Each node
  keeps its own `vec_embeddings` index (keyed by the local `embeddings.rowid`)
  through AFTER INSERT/UPDATE/DELETE triggers. These triggers also fire when
  cr-sqlite merges remote changes.
- cr-sqlite merges a new row column by column: the row is inserted with
  `vec = x''` and then updated. sqlite-vec 0.1.10a4 DiskANN raises
  "Could not fetch vector data" on a DELETE of a rowid it does not hold. The
  update trigger therefore deletes only when the old value was indexed. The
  first version of the trigger blocked replication of embeddings.
- `job_votes` is an extra table in 0001, used by the vote lease.

## Verdict against ADR 0006 requirements 1–7

| Requirement | Met? | Evidence |
|---|---|---|
| 1. Writes on more than one node | Yes | S1: all concurrent writes succeeded and converged in 15 ms. S2: 10 000 writes across two nodes with 0 errors. Resolution is per column by edit count, then value, not by time. |
| 2. Works with the laptop offline, catches up | Yes | S2: writes unaffected while node3 was down; it caught up on 10 005 rows in 3.6 s with an identical checksum. Catch-up needs the schema already migrated (S5). |
| 3. Vector tables and ANN | Partly | Stable sqlite-vec is brute force only. DiskANN exists only in 0.1.10 alphas (recall@10 0.31 by default, 0.65 at L=512 on random data). The vector index is local per node, rebuilt from replicated BLOBs by triggers; we wrote this ourselves and hit an alpha bug. |
| 4. Exactly-once scheduling | Only with custom work | A conditional-update lease duplicated 99 of 200 jobs under partition. A hand-built majority-vote lease gave 0 duplicates in both runs, but it depends on node3, has no retry or ballots, and is our own protocol. The engine offers no lock. |
| 5. Runs in Docker on a laptop | Yes | S6: 50–70 MB process RSS, under 1% idle CPU, 246 MB image. The DiskANN build is CPU-heavy (173 s for 20k vectors). |
| 6. Mature, with backup and restore | Partly / risky | Backup via `VACUUM INTO` restored with matching counts (S7), but the copy keeps the site id. cr-sqlite has had no release since 2024-01 and near-zero activity. The replication layer, the protocol and the lease are our own code. |
| 7. Alembic | Partly | Alembic drives each node with batch mode and `crsql_*_alter` wrappers. DDL does not replicate, so every node migrates itself, and nodes on different versions block each other's sync until they have all migrated. |
