"""TITAN DB spike: PostgreSQL + pgEdge Spock + pgvector. Runs S5, S1-S4, S6, S7.

Order: S5 runs first because it creates the schema (revision 0001 then 0002)
that every other scenario needs; S4 then runs against the HNSW index that 0002
created. Results go to results.json (raw) and out/ (logs, worker files).
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np
import psycopg
from psycopg.rows import dict_row
from alembic import command
from alembic.config import Config

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
PROJECT = "spike-pgedge"
NODES = ["node1", "node2", "node3"]
PORTS = {"node1": 16001, "node2": 16002, "node3": 16003, "restore": 16004}
SPOCK_NAME = {"node1": "n1", "node2": "n2", "node3": "n3"}
CLUSTER_NET = f"{PROJECT}_cluster"
DSN = "host=127.0.0.1 port={port} dbname=titan user=postgres password=spike connect_timeout=5"
RESULTS: dict = {"candidate": "PostgreSQL + pgEdge Spock + pgvector", "run_started": dt.datetime.now(dt.timezone.utc).isoformat()}


# ---------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({r.returncode}): {r.stderr.strip()[-800:]}")
    return r


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return sh("docker", "compose", "-p", PROJECT, "-f", str(HERE / "docker-compose.yml"), *args, check=check)


def container(node: str) -> str:
    return f"{PROJECT}-{node}-1"


def connect(node: str, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(DSN.format(port=PORTS[node]), autocommit=autocommit)


def q(node: str, sql: str, params=None):
    with connect(node) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else None


def q1(node: str, sql: str, params=None):
    rows = q(node, sql, params)
    return rows[0][0] if rows else None


def qdict(node: str, sql: str, params=None) -> list[dict]:
    with psycopg.connect(DSN.format(port=PORTS[node]), autocommit=True, row_factory=dict_row) as c:
        return [{k: (v if isinstance(v, (int, float, bool, type(None))) else str(v)) for k, v in r.items()}
                for r in c.execute(sql, params).fetchall()]


def safe(fn):
    """Call fn(); on a DB/connection error return the error text instead."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return f"ERROR {type(e).__name__}: {str(e).strip()[:200]}"


def wait_until(pred, timeout: float, interval: float = 0.05):
    """Return seconds until pred() is truthy, or None on timeout. Exceptions count as False."""
    t0 = time.perf_counter()
    while True:
        try:
            if pred():
                return time.perf_counter() - t0
        except Exception:
            pass
        if time.perf_counter() - t0 > timeout:
            return None
        time.sleep(interval)


def pct(xs: list[float], p: float) -> float:
    return float(np.percentile(np.array(xs), p)) if xs else float("nan")


TASKS_SUM = "SELECT count(*), coalesce(md5(string_agg(id::text || ':' || title, ',' ORDER BY id)), '') FROM tasks"
JOBS_SUM = ("SELECT count(*), coalesce(md5(string_agg(id::text || ':' || coalesce(lease_owner,'') || ':' || "
            "coalesce(done_at::text,''), ',' ORDER BY id)), '') FROM jobs")


def checksum(node: str, sql: str):
    return tuple(q(node, sql)[0])


def all_equal(sql: str, nodes=NODES) -> bool:
    sums = [checksum(n, sql) for n in nodes]
    return all(s == sums[0] for s in sums)


def spock_dsn(node: str) -> str:
    return f"host={SPOCK_NAME[node]}.cluster port=5432 dbname=titan user=postgres password=spike"


def sub_status(node: str):
    return q(node, "SELECT subscription_name, status FROM spock.sub_show_status()")


def all_subs_replicating() -> bool:
    return all(len(sub_status(n)) == 2 and all(st == "replicating" for _, st in sub_status(n)) for n in NODES)


def wait_subs(timeout: float = 120):
    el = wait_until(all_subs_replicating, timeout, 0.25)
    return None if el is None else round(el, 2)


def partition(node: str) -> None:
    sh("docker", "network", "disconnect", CLUSTER_NET, container(node))


def heal(node: str) -> None:
    sh("docker", "network", "connect", "--alias", f"{SPOCK_NAME[node]}.cluster", CLUSTER_NET, container(node))


def docker_logs_tail(node: str, n: int = 40) -> str:
    return sh("docker", "logs", "--tail", str(n), container(node), check=False).stderr


# ---------------------------------------------------------------- cluster
def bootstrap() -> dict:
    log("bootstrap: fresh cluster")
    compose("--profile", "restore", "down", "-v", "--remove-orphans", check=False)
    t0 = time.perf_counter()
    compose("up", "-d", "--wait", "node1", "node2", "node3")
    info = {"containers_healthy_s": round(time.perf_counter() - t0, 2)}
    info["versions"] = {
        "postgres": q1("node1", "SHOW server_version"),
        "spock_available": q1("node1", "SELECT default_version FROM pg_available_extensions WHERE name='spock'"),
        "pgvector_available": q1("node1", "SELECT default_version FROM pg_available_extensions WHERE name='vector'"),
    }
    # Manual step 1: extension + node on every node.
    for n in NODES:
        q(n, "CREATE EXTENSION IF NOT EXISTS spock")
        q(n, "SELECT spock.node_create(node_name := %s, dsn := %s)", (SPOCK_NAME[n], spock_dsn(n)))
    # Manual step 2: full mesh, 6 subscriptions, no forwarding (forward_origins '{}').
    for n in NODES:
        for m in NODES:
            if n != m:
                q(n, "SELECT spock.sub_create(subscription_name := %s, provider_dsn := %s, "
                     "synchronize_structure := false, synchronize_data := false)",
                  (f"sub_{SPOCK_NAME[n]}_{SPOCK_NAME[m]}", spock_dsn(m)))
    el = wait_until(lambda: all(all(s == "replicating" for _, s in sub_status(n)) and len(sub_status(n)) == 2
                                for n in NODES), 120, 0.5)
    info["subscriptions_replicating_s"] = None if el is None else round(el, 2)
    info["sub_status"] = {n: sub_status(n) for n in NODES}
    if el is None:
        raise RuntimeError(f"subscriptions not replicating: {info['sub_status']}\n{docker_logs_tail('node1')}")
    info["gucs"] = {r[0]: r[1] for r in q("node1", "SELECT name, setting FROM pg_settings WHERE name LIKE 'spock.%' "
                                               "OR name IN ('track_commit_timestamp','wal_level','shared_buffers')")}
    log(f"bootstrap done: {info['sub_status']}")
    return info


# ---------------------------------------------------------------- S5
def schema_state(node: str) -> dict:
    tables = [r[0] for r in q(node, "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")]
    cols = [r[0] for r in q(node, "SELECT column_name FROM information_schema.columns WHERE table_name='tasks' ORDER BY ordinal_position")]
    idx = [r[0] for r in q(node, "SELECT indexdef FROM pg_indexes WHERE tablename='embeddings' AND indexname='embeddings_vec_hnsw'")]
    ver = [r[0] for r in q(node, "SELECT version_num FROM alembic_version")] if "alembic_version" in tables else []
    ext = [r[0] for r in q(node, "SELECT extname || ' ' || extversion FROM pg_extension ORDER BY 1")]
    return {"tables": tables, "tasks_columns": cols, "hnsw_index": idx, "alembic_version": ver, "extensions": ext}


def repsets(node: str):
    return safe(lambda: q(node, "SELECT set_name, relname FROM spock.tables ORDER BY 2"))


def s5() -> dict:
    log("S5: alembic upgrade 0001_initial on node1")
    r: dict = {}
    cfg = Config(str(HERE / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://postgres:spike@127.0.0.1:16001/titan")
    t0 = time.perf_counter()
    command.upgrade(cfg, "0001_initial")
    r["upgrade_0001_s"] = round(time.perf_counter() - t0, 3)

    def has_0001(n):
        s = schema_state(n)
        return {"tasks", "jobs", "embeddings", "alembic_version"} <= set(s["tables"]) and s["alembic_version"] == ["0001_initial"]
    r["0001_visible_on_node2_node3_s"] = wait_until(lambda: has_0001("node2") and has_0001("node3"), 60)
    r["after_0001"] = {n: schema_state(n) for n in NODES}
    r["repsets_after_0001"] = {n: repsets(n) for n in NODES}

    log("S5: stop node3, alembic upgrade head on node1")
    compose("stop", "node3")
    t0 = time.perf_counter()
    command.upgrade(cfg, "head")
    r["upgrade_head_s"] = round(time.perf_counter() - t0, 3)

    def has_0002(n):
        s = schema_state(n)
        return "priority" in s["tasks_columns"] and s["hnsw_index"] and s["alembic_version"] == ["0002_priority_and_ann_index"]
    r["0002_visible_on_node2_s"] = wait_until(lambda: has_0002("node2"), 60)
    log("S5: start node3")
    t0 = time.perf_counter()
    compose("start", "node3")
    compose("up", "-d", "--wait", "node3")
    r["node3_healthy_after_start_s"] = round(time.perf_counter() - t0, 2)
    el = wait_until(lambda: has_0002("node3"), 120, 0.2)
    r["0002_on_node3_after_start_s"] = None if el is None else round(el + r["node3_healthy_after_start_s"], 2)
    r["final"] = {n: schema_state(n) for n in NODES}
    r["all_subs_replicating_s_after_0002_check"] = wait_subs()
    r["sub_status_final"] = {n: sub_status(n) for n in NODES}
    r["ok"] = all(has_0002(n) for n in NODES)
    log(f"S5 ok={r['ok']} node3 caught up in {r['0002_on_node3_after_start_s']} s")
    return r


# ---------------------------------------------------------------- S1
def s1_trial(i: int) -> dict:
    tid = uuid.uuid4()
    q("node1", "INSERT INTO tasks (id, owner, title, status, updated_at) VALUES (%s,'s1','original','open',now())", (tid,))
    wait_until(lambda: all(q1(n, "SELECT count(*) FROM tasks WHERE id=%s", (tid,)) == 1 for n in NODES), 30)
    conns = {"node1": connect("node1", autocommit=False), "node2": connect("node2", autocommit=False)}
    barrier = threading.Barrier(2)
    out: dict = {}

    def writer(node: str) -> None:
        c = conns[node]
        title = f"from-{node}-trial{i}"
        barrier.wait()
        t = time.perf_counter()
        try:
            xid = c.execute("UPDATE tasks SET title=%s, updated_at=clock_timestamp(), version=version+1 "
                            "WHERE id=%s RETURNING pg_current_xact_id()::text", (title, tid)).fetchone()[0]
            c.commit()
            lat = time.perf_counter() - t
            ts = c.execute("SELECT pg_xact_commit_timestamp((%s::bigint %% 4294967296)::text::xid)", (xid,)).fetchone()[0]
            c.commit()
            out[node] = {"ok": True, "title": title, "latency_ms": round(lat * 1000, 2), "commit_ts": ts.isoformat(),
                         "done_at": time.perf_counter()}
        except Exception as e:  # noqa: BLE001
            out[node] = {"ok": False, "title": title, "error": f"{type(e).__name__}: {e}", "done_at": time.perf_counter()}

    ths = [threading.Thread(target=writer, args=(n,)) for n in conns]
    [t.start() for t in ths]
    [t.join() for t in ths]
    t_both = max(v["done_at"] for v in out.values())
    conv = wait_until(lambda: len({q1(n, "SELECT title FROM tasks WHERE id=%s", (tid,)) for n in NODES}) == 1, 30, 0.005)
    conv_at = time.perf_counter()
    final = {n: q(n, "SELECT title, version, pg_xact_commit_timestamp(xmin)::text FROM tasks WHERE id=%s", (tid,))[0] for n in NODES}
    for c in conns.values():
        c.close()
    for v in out.values():
        v.pop("done_at")
    later = max((n for n in out if out[n]["ok"]), key=lambda n: out[n]["commit_ts"], default=None)
    winner = final["node1"][0]
    return {
        "task_id": str(tid), "writes": out,
        "final": {n: {"title": f[0], "version": f[1], "row_commit_ts": f[2]} for n, f in final.items()},
        "converged": conv is not None, "convergence_ms_after_both_commits": None if conv is None else round((conv_at - t_both) * 1000, 1),
        "winner_title": winner, "winner_is_later_commit_ts": later is not None and out[later]["title"] == winner,
    }


def s1(trials: int = 5) -> dict:
    log("S1: conflicting updates")
    wait_subs()
    before = {n: q1(n, "SELECT count(*) FROM spock.resolutions") for n in NODES}
    res = [s1_trial(i) for i in range(trials)]
    time.sleep(1)
    after = {n: q1(n, "SELECT count(*) FROM spock.resolutions") for n in NODES}
    r = {"trials": res,
         "spock_resolutions_rows_added": {n: after[n] - before[n] for n in NODES},
         "resolutions_sample": {n: qdict(n, "SELECT * FROM spock.resolutions ORDER BY 1 DESC LIMIT 3") for n in NODES},
         "conflict_resolution_guc": q1("node1", "SHOW spock.conflict_resolution")}
    log(f"S1: winners {[t['winner_title'] for t in res]} conv_ms {[t['convergence_ms_after_both_commits'] for t in res]}")
    return r


# ---------------------------------------------------------------- S2
def s2(n_rows: int = 10_000) -> dict:
    log("S2: stop node3, insert 10k tasks alternating node1/node2")
    r: dict = {}
    compose("stop", "node3")
    conns = {"node1": connect("node1"), "node2": connect("node2")}
    lat: dict = {"node1": [], "node2": []}
    errors: list[str] = []
    ok = {"node1": 0, "node2": 0}
    t0 = time.perf_counter()
    for i in range(n_rows):
        node = "node1" if i % 2 == 0 else "node2"
        t = time.perf_counter()
        try:
            conns[node].execute("INSERT INTO tasks (id, owner, title, status, updated_at) VALUES (%s,'s2',%s,'open',now())",
                                (uuid.uuid4(), f"s2 task {i}"))
            lat[node].append(time.perf_counter() - t)
            ok[node] += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{node}: {type(e).__name__}: {e}"[:200])
            conns[node] = connect(node)
    r["insert_wall_s"] = round(time.perf_counter() - t0, 2)
    all_lat = lat["node1"] + lat["node2"]
    r["writes_ok"] = ok
    r["write_errors"] = len(errors)
    r["write_error_samples"] = errors[:5]
    r["latency_ms"] = {"p50": round(pct(all_lat, 50) * 1000, 3), "p95": round(pct(all_lat, 95) * 1000, 3),
                       "p50_node1": round(pct(lat["node1"], 50) * 1000, 3), "p50_node2": round(pct(lat["node2"], 50) * 1000, 3)}
    for c in conns.values():
        c.close()
    el = wait_until(lambda: all_equal(TASKS_SUM, ["node1", "node2"]), 60, 0.2)
    r["node1_node2_converged_s_after_last_insert"] = el
    r["reference_checksum"] = list(checksum("node1", TASKS_SUM))
    r["wal_retained_for_node3_while_down"] = {n: q(n, "SELECT slot_name, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) "
                                                      "FROM pg_replication_slots WHERE slot_name LIKE %s", ("%n3%",)) for n in ["node1", "node2"]}
    log("S2: start node3")
    t0 = time.perf_counter()
    compose("start", "node3")
    wait_until(lambda: q1("node3", "SELECT 1") == 1, 60, 0.1)
    r["node3_accepting_connections_s"] = round(time.perf_counter() - t0, 2)
    ref = tuple(r["reference_checksum"])
    el = wait_until(lambda: checksum("node3", TASKS_SUM) == ref, 600, 0.2)
    r["node3_caught_up_s_from_start_command"] = None if el is None else round(time.perf_counter() - t0, 2)
    r["node3_checksum"] = list(checksum("node3", TASKS_SUM))
    r["identical_checksum"] = r["node3_checksum"] == r["reference_checksum"]
    r["all_subs_replicating_s_after_catchup"] = wait_subs()
    log(f"S2: p50={r['latency_ms']['p50']}ms p95={r['latency_ms']['p95']}ms errors={len(errors)} catch-up={r['node3_caught_up_s_from_start_command']}s")
    return r


# ---------------------------------------------------------------- S3
def s3_run(label: str, mode: str, do_partition: bool, n_jobs: int = 200, work_ms: int = 50) -> dict:
    log(f"S3 {label}: mode={mode} partition={do_partition} work_ms={work_ms}")
    r: dict = {"mode": mode, "partition": do_partition, "work_ms": work_ms, "settle_ms": 500 if mode == "settle" else 0}
    q("node1", "DELETE FROM jobs")
    wait_until(lambda: all(q1(n, "SELECT count(*) FROM jobs") == 0 for n in NODES), 30)
    with connect("node1", autocommit=False) as c:
        with c.cursor() as cur:
            cur.executemany("INSERT INTO jobs (id, kind, run_at) VALUES (%s, 'reminder', now())",
                            [(uuid.uuid4(),) for _ in range(n_jobs)])
        c.commit()
    wait_until(lambda: all(q1(n, "SELECT count(*) FROM jobs") == n_jobs for n in NODES), 30)
    d = OUT / f"s3_{label}"
    d.mkdir(parents=True, exist_ok=True)
    stop = d / "STOP"
    stop.unlink(missing_ok=True)
    files = {w: d / f"fired_{w}.csv" for w in ("w1", "w2")}
    for f in files.values():
        f.unlink(missing_ok=True)
        f.touch()
    procs = {w: subprocess.Popen([sys.executable, str(HERE / "worker.py"), "--port", str(port), "--name", w,
                                  "--out", str(files[w]), "--stop-file", str(stop), "--mode", mode,
                                  "--work-ms", str(work_ms)])
             for w, port in (("w1", 16001), ("w2", 16002))}
    t0 = time.perf_counter()

    def fired() -> int:
        return sum(len(f.read_text().splitlines()) for f in files.values())

    def undone(n: str) -> int:
        return q1(n, "SELECT count(*) FROM jobs WHERE done_at IS NULL")

    if do_partition:
        wait_until(lambda: fired() >= 100, 300, 0.01)
        r["fired_when_partitioned"] = fired()
        partition("node1")
        r["partitioned_at_s"] = round(time.perf_counter() - t0, 2)
        log(f"S3 {label}: node1 partitioned after {r['fired_when_partitioned']} fires")
        time.sleep(60)
        r["fired_during_partition_window"] = fired() - r["fired_when_partitioned"]
        r["undone_at_heal"] = {n: undone(n) for n in NODES}
        heal("node1")
        t_heal = time.perf_counter()
        log("S3: healed")
        el = wait_until(lambda: all_equal(JOBS_SUM) and all(undone(n) == 0 for n in NODES), 300, 0.5)
        r["converged_after_heal_s"] = None if el is None else round(time.perf_counter() - t_heal, 2)
        r["sub_status_after_heal"] = {n: sub_status(n) for n in NODES}
        r["all_subs_replicating_s_after_convergence"] = wait_subs()
    else:
        el = wait_until(lambda: all(undone(n) == 0 for n in ("node1", "node2")) and all_equal(JOBS_SUM), 300, 0.2)
        r["all_done_and_converged_s"] = None if el is None else round(time.perf_counter() - t0, 2)
    time.sleep(3)  # let workers observe "no due jobs"
    stop.touch()
    for p in procs.values():
        p.wait(timeout=60)
    r["wall_s"] = round(time.perf_counter() - t0, 2)
    counts: collections.Counter = collections.Counter()
    by_worker = {}
    for w, f in files.items():
        ids = [line.split(",")[0] for line in f.read_text().splitlines() if line]
        by_worker[w] = len(ids)
        counts.update(ids)
    job_ids = [str(x[0]) for x in q("node1", "SELECT id FROM jobs")]
    r["fired_exactly_once"] = sum(1 for j in job_ids if counts[j] == 1)
    r["fired_more_than_once"] = sum(1 for j in job_ids if counts[j] > 1)
    r["extra_fires"] = sum(counts[j] - 1 for j in job_ids if counts[j] > 1)
    r["never_fired"] = sum(1 for j in job_ids if counts[j] == 0)
    r["fires_by_worker"] = by_worker
    r["worker_stats"] = {w: json.loads(Path(str(files[w]) + ".stats.json").read_text()) for w in files}
    r["final_undone"] = {n: undone(n) for n in NODES}
    r["final_jobs_converged"] = all_equal(JOBS_SUM)
    log(f"S3 {label}: once={r['fired_exactly_once']} dup={r['fired_more_than_once']} never={r['never_fired']}")
    return r


def s3() -> dict:
    return {
        "a_healthy_settle": s3_run("a_settle", "settle", False),
        "a_healthy_naive": s3_run("a_naive", "naive", False),
        "a_healthy_naive_no_work_delay": s3_run("a_naive_nowork", "naive", False, work_ms=0),
        "b_partition_settle": s3_run("b_settle", "settle", True),
        "b_partition_naive": s3_run("b_naive", "naive", True),
    }


# ---------------------------------------------------------------- S4
def vec_text(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in v) + "]"


def s4(n: int = 20_000, dim: int = 384, nq: int = 100) -> dict:
    log("S4: vectors")
    r: dict = {}
    X = np.random.default_rng(42).standard_normal((n, dim)).astype(np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    Q = np.random.default_rng(7).standard_normal((nq, dim)).astype(np.float32)
    Q /= np.linalg.norm(Q, axis=1, keepdims=True)
    ids = [uuid.UUID(int=i + 1) for i in range(n)]
    pos = {str(u): i for i, u in enumerate(ids)}
    r["index_present_before_insert"] = q("node1", "SELECT indexdef FROM pg_indexes WHERE indexname='embeddings_vec_hnsw'")
    t0 = time.perf_counter()
    with connect("node1", autocommit=False) as c:
        for b in range(0, n, 1000):
            with c.cursor().copy("COPY embeddings (id, entity_id, chunk, vec) FROM STDIN") as cp:
                for i in range(b, min(b + 1000, n)):
                    cp.write_row((ids[i], uuid.UUID(int=10**9 + i // 4), i % 4, vec_text(X[i])))
            c.commit()
    r["insert_with_hnsw_s_node1"] = round(time.perf_counter() - t0, 2)
    t_ins = time.perf_counter()
    el = wait_until(lambda: q1("node2", "SELECT count(*) FROM embeddings") == n, 600, 0.2)
    r["replicated_to_node2_s_after_insert"] = None if el is None else round(time.perf_counter() - t_ins, 2)
    r["node3_count"] = q1("node3", "SELECT count(*) FROM embeddings") if wait_until(
        lambda: q1("node3", "SELECT count(*) FROM embeddings") == n, 600, 0.5) is not None else "timeout"
    r["hnsw_index_size"] = {nd: q1(nd, "SELECT pg_size_pretty(pg_relation_size('embeddings_vec_hnsw'))") for nd in NODES}
    # Standalone build time: a second, local-only HNSW index on node1 (DDL replication off in this session).
    with connect("node1") as c:
        c.execute("SET spock.enable_ddl_replication = off")
        t = time.perf_counter()
        c.execute("CREATE INDEX embeddings_vec_hnsw_buildtest ON embeddings USING hnsw (vec vector_cosine_ops)")
        r["standalone_hnsw_build_s_node1"] = round(time.perf_counter() - t, 2)
        c.execute("DROP INDEX embeddings_vec_hnsw_buildtest")
    # Exact ground truth
    truth = np.argsort(-(Q @ X.T), axis=1)[:, :10]
    with connect("node2") as c:
        plan = c.execute("EXPLAIN SELECT id FROM embeddings ORDER BY vec <=> %s::vector LIMIT 10", (vec_text(Q[0]),)).fetchall()
        r["node2_plan"] = [p[0][:120] for p in plan]
        # Sanity check of the ground truth: exact scan in Postgres (index off) for 10 queries.
        c.execute("SET enable_indexscan = off")
        exact = []
        for qi in range(10):
            got = c.execute("SELECT id FROM embeddings ORDER BY vec <=> %s::vector LIMIT 10", (vec_text(Q[qi]),)).fetchall()
            exact.append(len({pos[str(g[0])] for g in got} & set(truth[qi].tolist())) / 10)
        r["exact_scan_vs_numpy_truth_recall_10_queries"] = float(np.mean(exact))
        c.execute("RESET enable_indexscan")
        for ef in (40, 100, 400):
            c.execute(f"SET hnsw.ef_search = {ef}")
            lats, recalls = [], []
            for qi in range(nq):
                qt = vec_text(Q[qi])
                t = time.perf_counter()
                got = c.execute("SELECT id FROM embeddings ORDER BY vec <=> %s::vector LIMIT 10", (qt,)).fetchall()
                lats.append(time.perf_counter() - t)
                gi = {pos[str(g[0])] for g in got}
                recalls.append(len(gi & set(truth[qi].tolist())) / 10)
            r[f"ef_search_{ef}"] = {"p50_ms": round(pct(lats, 50) * 1000, 2), "p95_ms": round(pct(lats, 95) * 1000, 2),
                                    "recall_at_10": round(float(np.mean(recalls)), 4)}
    r["usable_on_node2_without_extra_steps"] = any("embeddings_vec_hnsw" in p for p in r["node2_plan"])
    log(f"S4: {r['ef_search_40']} build={r['standalone_hnsw_build_s_node1']}s")
    return r


# ---------------------------------------------------------------- S6
def s6() -> dict:
    log("S6: footprint (idle 20 s first)")
    time.sleep(20)
    r: dict = {}
    stats = sh("docker", "stats", "--no-stream", "--format", "{{json .}}", *[container(n) for n in NODES]).stdout
    for line in stats.splitlines():
        s = json.loads(line)
        node = s["Name"].split("-")[-2]
        r[node] = {"mem": s["MemUsage"], "mem_pct": s["MemPerc"], "cpu_pct": s["CPUPerc"]}
    for n in NODES:
        r[n]["pgdata_du"] = sh("docker", "exec", container(n), "du", "-sh", "/var/lib/postgresql/data").stdout.split()[0]
        r[n]["db_size_titan"] = q1(n, "SELECT pg_size_pretty(pg_database_size('titan'))")
        r[n]["rows"] = {t: q1(n, f"SELECT count(*) FROM {t}") for t in ("tasks", "jobs", "embeddings")}
    img = sh("docker", "compose", "-p", PROJECT, "-f", str(HERE / "docker-compose.yml"), "config", "--images").stdout.split()[0]
    r["image"] = img
    r["image_content_size_mb"] = round(int(sh("docker", "image", "inspect", img, "--format", "{{.Size}}").stdout.strip()) / 1e6, 1)
    r["image_disk_usage"] = json.loads(sh("docker", "image", "ls", img, "--format", "{{json .}}").stdout.splitlines()[0])["Size"]
    log(f"S6: {r}")
    return r


# ---------------------------------------------------------------- S7
def s7() -> dict:
    log("S7: pg_dump node1 -> restore into fresh single node")
    r: dict = {"method": "pg_dump -Fc -n public (logical) from node1; pg_restore into a fresh node without Spock"}
    t0 = time.perf_counter()
    sh("docker", "exec", container("node1"), "pg_dump", "-U", "postgres", "-d", "titan", "-Fc", "-n", "public", "-f", "/tmp/titan.dump")
    r["dump_s"] = round(time.perf_counter() - t0, 2)
    sh("docker", "cp", f"{container('node1')}:/tmp/titan.dump", str(OUT / "titan.dump"))
    r["dump_size_mb"] = round((OUT / "titan.dump").stat().st_size / 1e6, 1)
    listing = sh("docker", "exec", container("node1"), "pg_restore", "-l", "/tmp/titan.dump").stdout
    r["dump_contains_create_extension_vector"] = "EXTENSION - vector" in listing
    compose("--profile", "restore", "up", "-d", "--wait", "restore")
    sh("docker", "cp", str(OUT / "titan.dump"), f"{container('restore')}:/tmp/titan.dump")
    steps = []
    if not r["dump_contains_create_extension_vector"]:
        q("restore", "CREATE EXTENSION vector")
        steps.append("CREATE EXTENSION vector before pg_restore (-n public does not dump extensions)")
    t0 = time.perf_counter()
    rr = sh("docker", "exec", container("restore"), "pg_restore", "-U", "postgres", "-d", "titan", "--no-owner", "/tmp/titan.dump", check=False)
    r["restore_s"] = round(time.perf_counter() - t0, 2)
    r["pg_restore_rc"] = rr.returncode
    r["pg_restore_stderr"] = rr.stderr.strip()[-600:]
    r["manual_steps"] = steps
    tables = ["tasks", "jobs", "embeddings", "alembic_version"]
    src = {t: q1("node1", f"SELECT count(*) FROM {t}") for t in tables}
    dst = {t: safe(lambda t=t: q1("restore", f"SELECT count(*) FROM {t}")) for t in tables}
    r["row_counts_node1"] = src
    r["row_counts_restored"] = dst
    r["counts_match"] = src == dst
    r["tasks_checksum_match"] = checksum("node1", TASKS_SUM) == safe(lambda: checksum("restore", TASKS_SUM))
    r["restored_hnsw_index"] = safe(lambda: q("restore", "SELECT indexname FROM pg_indexes WHERE indexname='embeddings_vec_hnsw'"))
    log(f"S7: counts_match={r['counts_match']}")
    return r


# ---------------------------------------------------------------- main
def main() -> None:
    OUT.mkdir(exist_ok=True)
    only = set(sys.argv[1:])
    steps = [("S5", s5), ("S1", s1), ("S2", s2), ("S3", s3), ("S4", s4), ("S6", s6), ("S7", s7)]
    RESULTS["bootstrap"] = bootstrap()
    for name, fn in steps:
        if only and name not in only:
            continue
        t0 = time.perf_counter()
        try:
            RESULTS[name] = fn()
        except Exception as e:  # noqa: BLE001
            log(f"{name} FAILED: {e}")
            RESULTS[name] = {"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc(),
                             "node_logs": {n: docker_logs_tail(n) for n in NODES}}
        RESULTS[name]["scenario_wall_s"] = round(time.perf_counter() - t0, 1)
        (HERE / "results.json").write_text(json.dumps(RESULTS, indent=1, default=str))
    RESULTS["run_finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
    (HERE / "results.json").write_text(json.dumps(RESULTS, indent=1, default=str))
    for n in NODES:
        (OUT / f"{n}.log").write_text(sh("docker", "logs", container(n), check=False).stderr)
    log("done")


if __name__ == "__main__":
    main()
