# PostgreSQL + pgEdge Spock + pgvector spike results

Versions: PostgreSQL 17.11 (tag `REL_17_11` patched with the four
`spock/patches/17/*.diff` files) + Spock 5.0.11 + pgvector 0.8.6, built from
source on `ubuntu:24.04` (own image `titan-spike-pgedge:local`, 220 MB on disk).
Harness: Python 3.11, psycopg 3.3.6, SQLAlchemy 2.0.54, Alembic 1.20.0,
pgvector-python 0.5.0, numpy 2.4.6. Docker 29.3.1, Compose v5.1.1.
Run date: 2026-09-24. Host: shared 4-core container, so timings are indicative.

## Summary

Spock gives what ADR 0006 asks for on requirements 1, 2, 3, 5 and 7. Every node
accepted writes. With node3 stopped, 10 000 inserts on node1 and node2 all
succeeded, and node3 caught up in 1.9 s. pgvector's HNSW index and plain,
unmodified Alembic revisions reached every node through Spock's automatic DDL
replication, including the node that was offline during `upgrade head`.
Requirement 4 is where it fails. Replication is asynchronous and conflicts are
resolved by last-update-wins. `FOR UPDATE SKIP LOCKED` only protects against
workers on the same node, so the naive lease fired 17 to 44 of 200 jobs twice
even on a healthy cluster. A claim, settle and verify lease gave 200/200
exactly-once while the cluster was healthy, but fired 100 jobs twice during a
60 s partition. The two surprises were practical ones. Spock needs a patched
Postgres, so stock `postgres` images and distro packages cannot be used, and
PostgreSQL 17.11 added an `output_plugin_libraries` allow-list that silently
blocked every Spock subscription until `spock_output` was added to it. That
setting is not in the v5.0.11 docs; only the README on Spock's `main` branch
mentions it. After the partition healed, replication took about 56 s to resume
because Spock waits for a dead TCP connection to time out.

## How the cluster is built

- **Image.** pgEdge's current images are `ghcr.io/pgedge/pgedge-postgres`
  (source: https://github.com/pgEdge/postgres-images). They are built from
  pgEdge Enterprise packages hosted on `dnf.pgedge.com`. From this host both
  hosts return 403 at the egress proxy (`pkg-containers.githubusercontent.com`
  and `dnf.pgedge.com`). The older Docker Hub image `pgedge/pgedge` is marked
  "no longer actively maintained" (https://github.com/pgEdge/pgedge-docker)
  and stops at Spock 5.0.1. So the `Dockerfile` follows Spock's documented
  build-from-source procedure
  (https://github.com/pgEdge/spock/blob/v5.0.11/docs/install_spock.md): clone
  `postgres` at `REL_17_11`, apply `spock/patches/17/*.diff`, build it, then
  build `spock` v5.0.11 and `pgvector` v0.8.6. The versions match pgEdge's own
  package list `pg17.11-spock5.0.11-standard`, except that list has pgvector
  0.8.5. Build time was about 10 minutes, mostly slow `apt` downloads.
- **Configuration** (`docker/spike.conf`): `wal_level=logical`,
  `track_commit_timestamp=on`, `shared_preload_libraries='spock'`,
  `output_plugin_libraries='pgoutput, test_decoding, spock_output'`,
  `spock.enable_ddl_replication=on`, `spock.include_ddl_repset=on`,
  `spock.allow_ddl_from_functions=on`,
  `spock.conflict_resolution=last_update_wins`, `spock.save_resolutions=on`,
  and `shared_buffers=128MB`. Each container is limited to 1 GB.
- **Networks.** Each node has a `cluster`-only alias (`n1.cluster`,
  `n2.cluster`, `n3.cluster`) and every Spock DSN uses it. Published ports go
  through `client` (`gw_priority`). A partition is
  `docker network disconnect spike-pgedge_cluster <node>`. Healing is
  `docker network connect --alias nX.cluster …`, and the alias has to be given
  again because a reconnect does not restore it.
- **Mesh** (`harness.py: bootstrap`), run once per node:
  `CREATE EXTENSION spock` and `spock.node_create('nX', dsn)`. Then six
  `spock.sub_create('sub_nX_nY', provider_dsn, synchronize_structure:=false, synchronize_data:=false)`
  calls with the default replication sets `{default, default_insert_only, ddl_sql}`
  and `forward_origins '{}'`. All six subscriptions were `replicating` 0.08 s
  after creation.

### How DDL is replicated (S5 depends on this)

With `spock.enable_ddl_replication=on`, every DDL statement run on a node is
captured and sent through the `ddl_sql` replication set. With
`spock.include_ddl_repset=on`, a new table with a primary key is added to the
`default` set on each node when it is created there (tables without a PK go to
`default_insert_only`). So Alembic needs no Spock calls: `CREATE EXTENSION
vector`, `CREATE TABLE`, `ALTER TABLE ADD COLUMN`, `CREATE INDEX … USING hnsw`,
and the `alembic_version` insert and update all replicate. After `0001`, every
node listed `alembic_version`, `embeddings`, `jobs` and `tasks` in the
`default` set (`spock.tables`). The alternative is
`spock.replicate_ddl(command)`, which runs a statement locally and queues it
for the other nodes. The spike does not use it. Spock's documented cautions
(`docs/managing/spock_autoddl.md`): `CREATE TABLE … AS` replicates before the
table joins a replication set, `DROP TABLE` can cause problems in clusters of
three or more nodes, and auto-DDL should only be switched on when every node
has the same schema. `spock.enable_ddl_replication` is `PGC_USERSET`, so one
session can turn it off (S4 uses that for a local-only test index).

## Scenarios

The harness runs S5 first, because it creates the schema everything else
uses. It then runs S1, S2, S3, S4, S6 and S7 against one cluster. Numbers come
from the final full run (`results.json`, logs in `out/`).

### S1: conflicting writes

**Setting.** `spock.conflict_resolution = last_update_wins`, the only value
compiled into 5.0.x (`error`, `apply_remote`, `keep_local` and
`first_update_wins` are commented out in `src/spock.c`). When a remote change
hits a row last written by another origin, Spock keeps whichever version has
the newer commit timestamp (`track_commit_timestamp` must be on). It logs the
conflict and, with `save_resolutions=on`, writes a row to `spock.resolutions`
(`conflict_type=update_update`, `conflict_resolution=keep_local|apply_remote`,
plus both tuples and timestamps).

**Method.** Insert one task on node1 and wait until all three nodes have it.
Two threads, released by one barrier, run
`UPDATE tasks SET title=…, updated_at=clock_timestamp(), version=version+1`
on node1 and node2 with different titles. The run did 5 trials.

| Trial | node1 write | node2 write | Winner on all 3 nodes | Final `version` | Converged (ms after both commits) |
|---|---|---|---|---|---|
| 0 | ok, 12.8 ms | ok, 3.2 ms | node1 (later commit) | 3 | 49.5 |
| 1 | ok, 3.6 ms | ok, 3.4 ms | node1 (later commit) | 2 | 50.4 |
| 2 | ok, 4.6 ms | ok, 4.4 ms | node1 (later commit) | 2 | 45.7 |
| 3 | ok, 4.6 ms | ok, 4.2 ms | node2 (later commit) | 2 | 27.1 |
| 4 | ok, 10.7 ms | ok, 3.4 ms | node1 (later commit) | 3 | 31.3 |

- Both writes succeeded in every trial. Neither client saw an error or needed
  a retry.
- All three nodes converged in every trial, within 27–50 ms of the second
  commit. In every trial the winner was the write with the later commit
  timestamp.
- In trials 1–3 both nodes read `version=1` and wrote `version=2`, so it was a
  real update/update conflict. The losing title disappeared without any error:
  a lost update. In trials 0 and 4, node1's `UPDATE` took 10–13 ms because it
  waited for the Spock apply of node2's change, then updated on top of it
  (`version=3`). So there was no conflict in those two.
- `spock.resolutions` gained 2 rows on node1, 1 on node2 and 2 on node3.
- Consequence for TITAN: a row-level last-update-wins rule decides conflicts.
  Per-column merges would need Spock's delta-apply columns (numeric counters
  only) or an application-level design.

### S2: laptop offline and catch-up

`docker compose stop node3`, then 10 000 single-row autocommit inserts
alternating between node1 and node2.

- Writes: 5 000 ok on node1 and 5 000 ok on node2, **0 errors**. Writes on
  node1 and node2 were unaffected while node3 was down.
- Latency: p50 **0.777 ms**, p95 **2.039 ms** (10.7 s wall).
- While node3 was down, node1 and node2 each kept about 3.5 MB of WAL in node3's
  replication slot (`spk_titan_n1_sub_n3_n1`: 3521 kB).
- `docker compose start node3`: node3 accepted connections after 0.67 s and
  had all 10 005 rows with an identical checksum (`md5` of sorted `id:title`)
  **1.94 s** after the start command. All six subscriptions were
  `replicating` again 1.71 s later.
- Not tested, but relevant for a laptop that is away for weeks: a slot keeps
  WAL until its subscriber returns. `max_slot_wal_keep_size` is unlimited by
  default, so retention on the always-on nodes needs a cap and a plan to
  rebuild the laptop if the cap is hit.

### S3: exactly-once jobs

Claim technique, used by every worker (`worker.py`):

```sql
UPDATE jobs SET lease_owner = :me, lease_until = now() + interval '30 seconds'
 WHERE id = (SELECT id FROM jobs
              WHERE done_at IS NULL AND run_at <= now()
                AND (lease_until IS NULL OR lease_until < now())
              ORDER BY run_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING id
```

Then the worker fires (appends `<job id>,<worker>` to a local CSV and fsyncs),
and runs `UPDATE jobs SET done_at=now() WHERE id=… AND lease_owner=:me`.
Each fire includes 50 ms of simulated "work". Two modes:

- **naive**: fire right after the claim commits.
- **settle** (the best available technique): after the claim commits, wait
  500 ms, which is far above the observed 27–50 ms replication lag. Then
  re-read the row and fire only if `lease_owner` is still this worker.
  Concurrent claims on two nodes are an update/update conflict. Last-update-wins
  resolves it the same way on every node, so once both updates have been
  delivered only one claimant still holds the lease. This depends on
  replication delivering within the settle window. The engine gives no such
  guarantee.

Workers: w1 against node1, w2 against node2, 200 jobs per run.

| Run | Exactly once | More than once | Never | Worker errors | Notes |
|---|---|---|---|---|---|
| (a) healthy, **settle** | **200** | 0 | 0 | 0 | 100/100 split, 0 lost races |
| (a) healthy, naive | 183 | 17 | 0 | 0 | 17 `done` updates matched 0 rows (lease overwritten by LWW) |
| (a) healthy, naive, 0 ms work | 156 | 44 | 0 | 0 | extra stress run |
| (b) partition, **settle** | 100 | **100** | 0 | 0 | node1 cut off after 100 fires; each side fired all 100 remaining jobs |
| (b) partition, naive | 70 | 130 | 0 | 0 | |

- `SKIP LOCKED` row locks exist only on the local node. Spock does not
  replicate them, so the naive lease gives duplicates even on a healthy
  cluster. In a development run the same naive configuration happened to give
  0 duplicates, so the outcome depends on timing.
- Under the partition, both sides treat the other side's jobs as due, and
  every remaining job fires twice. No lease design can avoid this with
  asynchronous multi-master replication. When the partition healed, the
  `jobs` rows converged under last-update-wins (0 undone rows on any node, all
  checksums equal).
- **Recovery after heal: 56.3 s (settle) and 58.1 s (naive)** until `jobs`
  converged. The logs show why. node2's walsender for node1 hit
  `terminating walsender process due to replication timeout` 60 s after the
  partition. node2's apply worker for `sub_n2_n1` only reported
  `connection to other side has died` about 116 s after the partition, then
  reconnected within 20 ms. The Spock 6.0 release notes mention replacing this
  timeout-based detection with TCP keepalive.
- What TITAN would need: a single owner per job. Examples are "only the home
  server's scheduler fires reminders", or jobs partitioned by owning node with
  explicit handover. The shared `jobs` table then replicates state but does not
  arbitrate claims.

### S4: vector search

Revision `0002` created `embeddings_vec_hnsw` (HNSW, `vector_cosine_ops`,
default `m=16`, `ef_construction=64`) before any data existed. 20 000 unit
vectors (`default_rng(42)`, 384 dimensions) were inserted on node1 with `COPY`
in transactions of 1 000 rows.

- Insert on node1 with the HNSW index maintained incrementally: **98.7 s**
  (137.6 s in a development run). All 20 000 rows were on node2 10.3 s after
  the insert finished, and on node3 as well.
- Standalone index build time: `CREATE INDEX … USING hnsw` on the loaded
  table (node1, local only via `SET spock.enable_ddl_replication=off`) took
  **20.2 s**. The index is 39 MB on each node.
- On node2, `EXPLAIN` shows `Index Scan using embeddings_vec_hnsw`. The
  vectors and index were **usable on node2 without extra steps**. Each node
  maintains its own index as replicated rows arrive.
- 100 top-10 queries on node2 (`default_rng(7)`), with recall computed against
  numpy brute force. As a sanity check, an exact scan in Postgres with the
  index disabled gave recall 1.0 against the numpy truth.

| `hnsw.ef_search` | p50 | p95 | recall@10 |
|---|---|---|---|
| 40 (default) | **2.26 ms** | **3.61 ms** | **0.220** |
| 100 | 3.72 ms | 4.20 ms | 0.388 |
| 400 | 10.28 ms | 21.06 ms | 0.794 |

The low recall at the default setting comes from the dataset: isotropic
random 384-dimensional vectors are a known worst case for HNSW. Replication
plays no part, and every node holds the same data. Real embeddings will
behave differently. This was not measured here.

### S5: Alembic across the cluster

- `alembic upgrade 0001_initial` against node1 only took 0.065 s. The schema
  (the `vector` extension, the three tables, `alembic_version` =
  `0001_initial`) was on node2 and node3 **0.28 s** later.
- `docker compose stop node3`, then `alembic upgrade head` on node1 took
  0.038 s. node2 had `priority`, `embeddings_vec_hnsw` and `alembic_version` =
  `0002_priority_and_ann_index` 0.11 s later.
- `docker compose start node3`: node3 was healthy after 2.89 s and had the new
  column, the HNSW index and `alembic_version` =
  `0002_priority_and_ann_index` at **2.96 s**. All subscriptions were
  `replicating` 1.17 s after that.
- Manual or engine-specific steps: none in Alembic. `env.py` and both
  revisions are plain Alembic. The one-time cluster setup is `CREATE EXTENSION
  spock`, `node_create` and `sub_create` on each node, plus the auto-DDL
  settings in `postgresql.conf`. The rule to follow: run migrations against
  exactly one node, and never run the same migration on two nodes (it would
  replicate twice). This spike did not test DDL on two nodes at once.

### S6: footprint

Measured after S1–S5, 20 s idle, with 10 005 tasks, 200 jobs and 20 000
embeddings loaded. `docker stats --no-stream` is a single sample.

| Node | Memory | CPU | PGDATA (`du`) | DB `titan` |
|---|---|---|---|---|
| node1 | 305.4 MiB | 2.40 % | 264 MB | 81 MB |
| node2 | 229.1 MiB | 0.43 % | 217 MB | 81 MB |
| node3 | 207.9 MiB | 0.01 % | 217 MB | 81 MB |

Image: 220 MB on disk (57.2 MB content size) for the self-built image. The
official `standard` image with PostGIS, Patroni and more is larger, but it
could not be pulled here. Settings: `shared_buffers=128MB`, `mem_limit: 1g`.

### S7: backup and restore

Supported methods: all standard Postgres tools work per node (`pg_dump`,
`pg_basebackup`). pgEdge documents pgBackRest, which ships in its standard
image, and a pgBackRest-based node add
(`docs/modify/add_node_pgbackrest.md`). Tested here: `pg_dump -Fc -n public`
on node1 took 8.84 s (37.4 MB), and `pg_restore --no-owner` into a fresh
single node with Spock not preloaded took 24.6 s, most of it rebuilding HNSW.
One manual step: `CREATE EXTENSION vector` before restoring, because `-n
public` does not dump extensions. `pg_restore` reported one ignorable error
(`schema "public" already exists`). **Row counts matched** for `tasks` 10 005,
`jobs` 200, `embeddings` 20 000 and `alembic_version` 1. The `tasks` checksum
matched and the HNSW index was recreated. Not tested: bringing a restored node
back into the Spock mesh. That needs Spock's add-node (Zodan) procedure, not a
plain restore.

### S8: licence and project health

| Component | Licence | Latest release (date) | Conditions for free self-hosting | Source |
|---|---|---|---|---|
| PostgreSQL | PostgreSQL License | 17.11 used, tag `REL_17_11` stamped 2026-08-10; `REL_18_6` also tagged | None beyond keeping the copyright notice | https://github.com/postgres/postgres/blob/REL_17_11/COPYRIGHT, https://github.com/postgres/postgres/tree/REL_17_11 |
| Spock | PostgreSQL License (`LICENSE.md`, "Copyright (c) 2021 - 2025, pgEdge, Inc.") | v5.0.11 (tag commit dated 2026-07-29); v6.0.0-beta.1 tag (commit dated 2026-07-01). No GitHub Releases, only tags plus `docs/spock_release_notes.md`. `main` had a commit on 2026-09-23 | None beyond the notice. Up to 4.x it was the source-available **pgEdge Community License 1.0** (Confluent-derived, forbids competing SaaS) | https://github.com/pgEdge/spock/blob/v5.0.11/LICENSE.md, https://github.com/pgEdge/spock/blob/v4.0.10/PGEDGE-COMMUNITY-LICENSE.md, https://github.com/pgEdge/spock/blob/v5.0.11/docs/spock_release_notes.md, https://github.com/pgEdge/spock/releases |
| pgvector | PostgreSQL License | 0.8.6 (2026-07-29) | None beyond the notice | https://github.com/pgvector/pgvector/blob/v0.8.6/LICENSE, https://github.com/pgvector/pgvector/blob/v0.8.6/CHANGELOG.md |
| pgEdge container images | PostgreSQL-style permissive licence | `ghcr.io/pgedge/pgedge-postgres` (changelog lists 17.11 / Spock 5.0.11) | Same | https://github.com/pgEdge/postgres-images/blob/main/LICENSE, https://github.com/pgEdge/postgres-images/blob/main/CHANGELOG.md |

A pgEdge press release announced the relicensing from the pgEdge Community
License to the PostgreSQL License in September 2025
(https://www.pgedge.com/press-releases/announcing-pgedge-enterprise-postgres-alongside-full-commitment-to-open-source).
That page was found by search but could not be fetched from this host
(egress 403). The licence files in the repositories are the primary evidence.
Project health: Spock 5.0.x has had regular minor tags (5.0.4 in Oct 2025,
5.0.5 on 2026-02-12, then 5.0.6 to 5.0.11 by July 2026), and 6.0 is in beta.
One pgEdge company maintains it, not a community.

## Adaptations and manual steps

- **Types:** none needed. `uuid`, `timestamptz` and `vector(384)` are used as
  specified. The HNSW index uses `vector_cosine_ops`, and the vectors are unit
  length.
- **Build from source**, because neither the current pgEdge image nor its
  package repository could be reached (see above). Spock requires Postgres to
  be built with its patches, so a stock `postgres:17` image cannot be used.
- **`output_plugin_libraries`**: PostgreSQL 17.11 (and 15.19, 16.15, 18.6)
  only allow listed logical decoding plugins. Without `spock_output` in the
  list, every `sub_create` fails with `library "spock_output" may not be used
  as an output plugin` (seen in the first attempt). The v5.0.11 docs do not
  mention it; the README on Spock's `main` branch does.
- **One-time cluster setup:** `CREATE EXTENSION spock`, `spock.node_create`
  on each node, and 6 × `spock.sub_create` for the full mesh.
- **Network partition heal** must re-add the `cluster` alias
  (`docker network connect --alias`).
- **S3:** the "settle" lease is an application pattern, not an engine
  feature. Spock does not replicate row locks.
- **S3:** partitioning happens once 100 fires are observed. Workers keep
  polling until the harness stops them, so they kept running through the 60 s
  partition and the heal.
- **S4:** the standalone index-build test ran with DDL replication switched
  off in that session, so the test index stayed on node1. It was then dropped.
- **S7:** `CREATE EXTENSION vector` before `pg_restore`, and the restore target
  runs without Spock.

## Verdict against ADR 0006 requirements 1–7

| Requirement | Met? | Evidence |
|---|---|---|
| 1. Writes on more than one node | **Yes** | S1 and S2: all three nodes accept writes, with no errors or retries. Conflicts are resolved row-level last-update-wins by commit timestamp (lost update in 3 of 5 S1 trials) |
| 2. Works with laptop offline, laptop catches up | **Yes** | S2: 10 000/10 000 writes ok with node3 down, catch-up 1.94 s after start, identical checksum. S5: an offline node caught up on DDL. WAL retention for long absences needs a cap (not tested) |
| 3. Vectors + ANN | **Yes** | pgvector 0.8.6 HNSW, usable on node2 with no extra steps. p50 2.26 ms, recall@10 0.22 at default `ef_search` (0.79 at 400) on random data |
| 4. Exactly-once scheduler | **No (engine); only by design outside the engine** | S3: `SKIP LOCKED` is node-local. Naive lease: 17–44 duplicates while healthy. Settle lease: 0 duplicates healthy, 100 duplicates under partition. Needs single-owner scheduling (for example only the home server fires reminders) |
| 5. Runs in Docker on a laptop | **Yes** | S6: 208–305 MiB and 0–2.4 % CPU idle per node, 220 MB image. The caveat is the need for pgEdge's patched build: no stock image |
| 6. Mature, working backup/restore | **Partly** | Postgres core is mature. `pg_dump`/`pg_restore` worked with matching counts (S7). Spock is maintained by one vendor, was source-available until Sept 2025, and 6.0 is in beta. The 17.11 `output_plugin_libraries` break shows it is tied to Postgres minor versions. Rejoining a restored node needs Spock tooling, which was not tested |
| 7. Alembic | **Yes** | S5: unmodified Alembic, DDL auto-replicated to all nodes including one that was offline. The rule is to migrate against one node only |
