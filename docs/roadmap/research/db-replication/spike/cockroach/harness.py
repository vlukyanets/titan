"""CockroachDB spike harness for ADR 0006 (throwaway code).

Usage:
    python harness.py            # run all scenarios from a clean cluster, tear down
    python harness.py worker ... # S3 worker process (started by the harness)
Env: KEEP=1 leaves the cluster running at the end.
"""
from __future__ import annotations

import argparse
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

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
PROJECT = "spike-cockroach"
IMAGE = "cockroachdb/cockroach:v26.3.2"
PORTS = {"node1": 16201, "node2": 16202, "node3": 16203}
RESTORE_PORT = 16207
CLUSTER_NET = f"{PROJECT}_cluster"
ALIASES = {"node1": "n1-cluster", "node2": "n2-cluster", "node3": "n3-cluster"}

R: dict = {"candidate": "CockroachDB", "run_date": dt.date.today().isoformat(), "scenarios": {}}


# ----------------------------------------------------------------- helpers
def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def sh(*args, check=True, capture=True, timeout=600):
    p = subprocess.run(list(args), capture_output=capture, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args} failed rc={p.returncode}: {p.stderr}\n{p.stdout}")
    return p


def compose(*args, **kw):
    return sh("docker", "compose", "-p", PROJECT, "-f", str(HERE / "docker-compose.yml"),
              "--profile", "restore", *args, **kw)


def container(svc: str) -> str:
    return compose("ps", "-a", "-q", svc).stdout.strip()


def dsn(port: int, db: str = "titan", timeout: int = 5) -> str:
    return f"host=127.0.0.1 port={port} user=root dbname={db} sslmode=disable connect_timeout={timeout}"


def connect(node: str | int, db: str = "titan", timeout: int = 5, stmt_timeout: str | None = None):
    port = PORTS[node] if isinstance(node, str) else node
    c = psycopg.connect(dsn(port, db, timeout), autocommit=True)
    if stmt_timeout:
        c.execute(f"SET statement_timeout = '{stmt_timeout}'")
    return c


def q1(conn, sql, params=None):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def wait_sql(node: str | int, db="defaultdb", deadline=180.0) -> float:
    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < deadline:
        try:
            with connect(node, db, timeout=3) as c:
                c.execute("SELECT 1")
                return time.monotonic() - t0
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise TimeoutError(f"{node} not ready: {last}")


def pct(xs, p):
    return float(np.percentile(np.asarray(xs), p)) if xs else None


def save():
    (HERE / "results.json").write_text(json.dumps(R, indent=2, default=str) + "\n")


def checksum_sql(extra=""):
    return ("SELECT count(*), md5(coalesce(string_agg(id::STRING || ':' || title, ',' ORDER BY id), '')) "
            f"FROM tasks {extra}")


def err_code(e: Exception) -> str:
    return getattr(e, "sqlstate", None) or type(e).__name__


def alembic(*args, url: str):
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "alembic", "-c", str(HERE / "alembic.ini"),
                        "-x", f"url={url}", *args], capture_output=True, text=True, cwd=HERE, timeout=600)
    return {"cmd": "alembic " + " ".join(args), "rc": p.returncode, "seconds": round(time.monotonic() - t0, 2),
            "stdout": p.stdout.strip()[-3000:], "stderr": p.stderr.strip()[-3000:]}


def sa_url(node: str) -> str:
    return f"cockroachdb+psycopg://root@127.0.0.1:{PORTS[node]}/titan?sslmode=disable"


def scenario(name):
    def deco(fn):
        def run():
            log(f"===== {name} =====")
            t0 = time.monotonic()
            res: dict = {}
            R["scenarios"][name] = res
            try:
                fn(res)
                res.setdefault("status", "ran")
            except Exception as e:  # noqa: BLE001
                res["status"] = "error"
                res["error"] = f"{type(e).__name__}: {e}"
                res["traceback"] = traceback.format_exc()[-4000:]
                log("ERROR", name, e)
            res["scenario_seconds"] = round(time.monotonic() - t0, 1)
            save()
        return run
    return deco


# ----------------------------------------------------------------- cluster lifecycle
def cluster_up():
    log("clean start: compose down -v")
    compose("down", "-v", "--remove-orphans", check=False)
    OUT.mkdir(exist_ok=True)
    for f in (x for x in OUT.glob("*") if x.name != "run.log"):
        f.unlink()
    compose("up", "-d", "node1", "node2", "node3")
    time.sleep(3)
    t0 = time.monotonic()
    for _ in range(60):
        p = compose("exec", "-T", "node1", "./cockroach", "init", "--insecure", "--host=n1-cluster:26357",
                    check=False)
        if p.returncode == 0 or "already been initialized" in (p.stderr + p.stdout):
            break
        time.sleep(1)
    for n in PORTS:
        wait_sql(n)
    R["cluster_init_seconds"] = round(time.monotonic() - t0, 1)
    with connect("node1", "defaultdb") as c:
        c.execute("CREATE DATABASE IF NOT EXISTS titan")
        R["version"] = q1(c, "SELECT version()")
        R["node_ids"] = {}
        # v26.x restricts crdb_internal/system; the harness opts in for introspection only.
        c.execute("SET allow_unsafe_internals = true")
        for nid, addr in c.execute("SELECT node_id, address FROM crdb_internal.gossip_nodes ORDER BY node_id"):
            svc = {v: k for k, v in ALIASES.items()}[addr.split(":")[0]]
            R["node_ids"][svc] = nid
        R["defaults"] = {
            "isolation": q1(c, "SHOW transaction_isolation"),
            "autocommit_before_ddl": q1(c, "SHOW autocommit_before_ddl"),
            "diagnostics.reporting.enabled": q1(c, "SHOW CLUSTER SETTING diagnostics.reporting.enabled"),
            "feature.vector_index.enabled": q1(c, "SHOW CLUSTER SETTING feature.vector_index.enabled"),
            "enterprise.license": q1(c, "SHOW CLUSTER SETTING enterprise.license"),
            "num_replicas": q1(c, "SELECT raw_config_sql FROM [SHOW ZONE CONFIGURATION FROM RANGE default]"),
        }
    log("cluster up", R["node_ids"], R["version"])
    save()


def stop(*svcs):
    t0 = time.monotonic()
    compose("stop", *svcs)
    return round(time.monotonic() - t0, 2)


def start(*svcs):
    compose("start", *svcs)


def partition(svc):
    sh("docker", "network", "disconnect", CLUSTER_NET, container(svc))


def heal(svc):
    sh("docker", "network", "connect", "--alias", ALIASES[svc], CLUSTER_NET, container(svc))


def ranges_underreplicated() -> dict:
    out = {}
    for n in PORTS:
        try:
            with connect(n, timeout=3, stmt_timeout="5s") as c:
                c.execute("SET allow_unsafe_internals = true")
                out[n] = q1(c, "SELECT sum(value)::INT FROM crdb_internal.node_metrics "
                               "WHERE name IN ('ranges.underreplicated','ranges.unavailable')")
        except Exception as e:  # noqa: BLE001
            out[n] = f"err {err_code(e)}"
    return out


# ----------------------------------------------------------------- S5
@scenario("S5")
def s5(res):
    res["upgrade_0001_on_node1"] = alembic("upgrade", "0001_initial", url=sa_url("node1"))
    t0 = time.monotonic()
    seen = {}
    for n in ("node2", "node3"):
        with connect(n) as c:
            tabs = sorted(r[0] for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))
            ver = q1(c, "SELECT version_num FROM alembic_version")
            seen[n] = {"tables": tabs, "alembic_version": ver,
                       "embeddings.vec type": q1(c, "SELECT data_type FROM information_schema.columns "
                                                    "WHERE table_name='embeddings' AND column_name='vec'")}
    res["after_0001_seen_on"] = seen
    res["after_0001_check_seconds"] = round(time.monotonic() - t0, 2)

    res["stop_node3_seconds"] = stop("node3")
    res["upgrade_head_on_node1_with_node3_stopped"] = alembic("upgrade", "head", url=sa_url("node1"))
    t0 = time.monotonic()
    start("node3")
    res["node3_sql_ready_seconds"] = round(wait_sql("node3", "titan"), 2)
    with connect("node3") as c:
        res["node3_after_head"] = {
            "priority_column": c.execute(
                "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name='tasks' AND column_name='priority'").fetchall(),
            "indexes_on_embeddings": [list(r) for r in c.execute(
                "SELECT DISTINCT index_name FROM [SHOW INDEXES FROM embeddings]")],
            "create_table_embeddings": q1(c, "SELECT create_statement FROM [SHOW CREATE TABLE embeddings]"),
            "alembic_version": q1(c, "SELECT version_num FROM alembic_version"),
            "vector_index_setting": q1(c, "SHOW CLUSTER SETTING feature.vector_index.enabled"),
        }
    res["node3_alembic_current"] = alembic("current", url=sa_url("node3"))
    res["alembic_check_node1"] = alembic("check", url=sa_url("node1"))
    log(json.dumps(res["node3_after_head"], default=str)[:600])


# ----------------------------------------------------------------- S1
@scenario("S1")
def s1(res):
    tid = uuid.uuid4()
    with connect("node1") as c:
        c.execute("INSERT INTO tasks (id, owner, title, status, updated_at) VALUES (%s,'me','t0','open',now())",
                  (tid,))
    t0 = time.monotonic()
    for n in PORTS:
        with connect(n) as c:
            while q1(c, "SELECT count(*) FROM tasks WHERE id=%s", (tid,)) != 1:
                time.sleep(0.01)
    res["insert_visible_on_all_nodes_seconds"] = round(time.monotonic() - t0, 4)

    conns = {n: connect(n) for n in PORTS}

    def converge(expected_any):
        t0 = time.monotonic()
        while True:
            vals = {n: q1(conns[n], "SELECT title FROM tasks WHERE id=%s", (tid,)) for n in PORTS}
            if len(set(vals.values())) == 1:
                return vals, time.monotonic() - t0
            if time.monotonic() - t0 > 30:
                return vals, None

    def run_trials(mode, trials):
        out = []
        for i in range(trials):
            barrier = threading.Barrier(2)
            outcome = {}

            def writer(node):
                c = connect(node)
                val = f"{mode}-{i}-{node}"
                retries, errors = 0, []
                t_start = None
                succeeded = False
                while True:
                    try:
                        if mode == "autocommit":
                            if retries == 0:
                                barrier.wait()
                            t_start = time.monotonic()
                            c.execute("UPDATE tasks SET title=%s, version=version+1, updated_at=now() WHERE id=%s",
                                      (val, tid))
                            succeeded = True
                        else:  # explicit read-modify-write transaction, client retry loop
                            c.execute("BEGIN")
                            v = q1(c, "SELECT version FROM tasks WHERE id=%s", (tid,))
                            if retries == 0:
                                barrier.wait()
                                t_start = time.monotonic()
                            c.execute("UPDATE tasks SET title=%s, version=%s, updated_at=now() WHERE id=%s",
                                      (val, v + 1, tid))
                            c.execute("COMMIT")
                        succeeded = True
                        break
                    except psycopg.Error as e:
                        errors.append(err_code(e))
                        try:
                            c.execute("ROLLBACK")
                        except Exception:  # noqa: BLE001
                            pass
                        if err_code(e) == "40001" and retries < 20:
                            retries += 1
                            continue
                        break
                outcome[node] = {"value": val, "ok": succeeded,
                                 "retries": retries, "errors": errors,
                                 "latency_ms": round((time.monotonic() - t_start) * 1000, 2) if t_start else None,
                                 "t_end": time.monotonic()}
                c.close()

            ths = [threading.Thread(target=writer, args=(n,)) for n in ("node1", "node2")]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            vals, conv = converge(None)
            final = vals["node1"]
            winner = [n for n in ("node1", "node2") if outcome[n]["value"] == final]
            last_committer = max(("node1", "node2"), key=lambda n: outcome[n]["t_end"])
            out.append({"trial": i, "node1": {k: v for k, v in outcome["node1"].items() if k != "t_end"},
                        "node2": {k: v for k, v in outcome["node2"].items() if k != "t_end"},
                        "value_per_node": vals, "winner_write_from": winner[0] if winner else None,
                        "last_committer": last_committer,
                        "converged": conv is not None,
                        "convergence_ms_after_both_returned": round(conv * 1000, 2) if conv is not None else None,
                        "version_after": q1(conns["node3"], "SELECT version FROM tasks WHERE id=%s", (tid,))})
        return out

    for mode in ("autocommit", "explicit_rmw"):
        trials = run_trials(mode, 10)
        res[mode] = {
            "trials": trials,
            "trials_both_writes_succeeded": sum(1 for t in trials if t["node1"]["ok"] and t["node2"]["ok"]),
            "trials_with_any_client_error": sum(1 for t in trials if t["node1"]["errors"] or t["node2"]["errors"]),
            "total_client_retries_40001": sum(t["node1"]["retries"] + t["node2"]["retries"] for t in trials),
            "winner_counts": {n: sum(1 for t in trials if t["winner_write_from"] == n) for n in ("node1", "node2")},
            "winner_is_last_committer": sum(1 for t in trials if t["winner_write_from"] == t["last_committer"]),
            "all_converged": all(t["converged"] for t in trials),
            "max_convergence_ms": max(t["convergence_ms_after_both_returned"] or 0 for t in trials),
            "write_latency_ms_p50": pct([t[n]["latency_ms"] for t in trials for n in ("node1", "node2")], 50),
        }
        log(mode, {k: v for k, v in res[mode].items() if k != "trials"})
    for c in conns.values():
        c.close()


# ----------------------------------------------------------------- S2
@scenario("S2")
def s2(res):
    with connect("node1") as c:
        c.execute("DELETE FROM tasks WHERE true")
    res["stop_node3_seconds"] = stop("node3")
    time.sleep(2)
    conns = {n: connect(n, stmt_timeout="10s") for n in ("node1", "node2")}
    lat = {"node1": [], "node2": []}
    errors = []
    t0 = time.monotonic()
    for i in range(10_000):
        node = "node1" if i % 2 == 0 else "node2"
        tid = uuid.uuid4()
        for attempt in range(5):
            s = time.perf_counter()
            try:
                conns[node].execute("INSERT INTO tasks (id, owner, title, status, updated_at) "
                                    "VALUES (%s, 'me', %s, 'open', now())", (tid, f"task {i}"))
                lat[node].append((time.perf_counter() - s) * 1000)
                break
            except psycopg.Error as e:
                errors.append({"i": i, "node": node, "attempt": attempt, "code": err_code(e), "msg": str(e)[:200]})
                if conns[node].closed:
                    conns[node] = connect(node, stmt_timeout="10s")
    res["insert_seconds"] = round(time.monotonic() - t0, 1)
    all_lat = lat["node1"] + lat["node2"]
    res["writes_ok"] = len(all_lat)
    res["write_errors"] = len(errors)
    res["write_error_samples"] = errors[:10]
    res["latency_ms"] = {"p50": pct(all_lat, 50), "p95": pct(all_lat, 95),
                         "node1_p50": pct(lat["node1"], 50), "node1_p95": pct(lat["node1"], 95),
                         "node2_p50": pct(lat["node2"], 50), "node2_p95": pct(lat["node2"], 95)}
    ref_count, ref_md5 = conns["node1"].execute(checksum_sql()).fetchone()
    last_id, last_title = conns["node1"].execute(
        "SELECT id, title FROM tasks WHERE title = 'task 9999'").fetchone()
    ts_after = q1(conns["node1"], "SELECT cluster_logical_timestamp()")
    res["reference"] = {"count": ref_count, "md5": ref_md5}
    res["tasks_table_ranges"] = [list(r) for r in conns["node1"].execute(
        "SELECT range_id, lease_holder, replicas FROM [SHOW RANGES FROM TABLE tasks WITH DETAILS]")]
    res["underreplicated_or_unavailable_ranges_before_restart"] = ranges_underreplicated()
    log("S2 inserts", res["writes_ok"], "errors", res["write_errors"], res["latency_ms"])
    for c in conns.values():
        c.close()

    # Catch-up, three signals polled together from the moment `compose start node3` is issued:
    #  gateway: checksum through node3 as SQL gateway (reads go to the leaseholder, wherever it is)
    #  local:   node3's own replica serves a bounded-staleness point read of the last inserted row at a
    #           timestamp after all inserts (nearest_only=true => must be served locally, else error).
    #           tasks is a single range here (see tasks_table_ranges), so this implies node3's replica
    #           of the whole table caught up.
    #  metric:  ranges.underreplicated + ranges.unavailable == 0 on every node (gauges refresh periodically)
    ts_iso = dt.datetime.fromtimestamp(float(ts_after) / 1e9, dt.timezone.utc).isoformat()
    local_sql = (f"SELECT title FROM tasks AS OF SYSTEM TIME with_min_timestamp('{ts_iso}', true) "
                 "WHERE id = %s")
    marks = {"sql_ready": None, "gateway_checksum_match": None, "local_replica_point_read": None,
             "underreplicated_zero": None}
    local_err, last_metric = [], None
    t_start = time.monotonic()
    start("node3")
    c3 = None
    next_metric = 0.0
    while time.monotonic() - t_start < 300 and any(v is None for v in marks.values()):
        el = lambda: round(time.monotonic() - t_start, 2)  # noqa: E731
        if c3 is None or c3.closed:
            try:
                c3 = connect("node3", timeout=3, stmt_timeout="5s")
                marks["sql_ready"] = marks["sql_ready"] or el()
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
                continue
        try:
            if marks["gateway_checksum_match"] is None:
                if tuple(c3.execute(checksum_sql()).fetchone()) == (ref_count, ref_md5):
                    marks["gateway_checksum_match"] = el()
            if marks["local_replica_point_read"] is None:
                try:
                    if q1(c3, local_sql, (last_id,)) == last_title:
                        marks["local_replica_point_read"] = el()
                except psycopg.Error as e:
                    local_err.append(f"t={el()} {err_code(e)}: {str(e).splitlines()[0][:160]}")
        except psycopg.Error:
            pass
        if marks["underreplicated_zero"] is None and time.monotonic() >= next_metric:
            last_metric = ranges_underreplicated()
            if all(v == 0 for v in last_metric.values()):
                marks["underreplicated_zero"] = el()
            next_metric = time.monotonic() + 1.0
        time.sleep(0.25)
    if c3 is not None:
        c3.close()
    res["catch_up_seconds_after_start"] = marks
    res["local_read_errors_before_success"] = local_err[:5] + ([f"... {len(local_err)} total"] if len(local_err) > 5 else [])
    res["underreplicated_last_seen"] = last_metric
    log("S2 catch-up", marks, res["local_read_errors_before_success"][:2])


# ----------------------------------------------------------------- S3
CLAIM_SQL = """
UPDATE jobs SET lease_owner = %(w)s, lease_until = now() + INTERVAL '30 seconds'
WHERE id = (
    SELECT id FROM jobs
    WHERE done_at IS NULL AND run_at <= now() AND (lease_until IS NULL OR lease_until < now())
    ORDER BY run_at LIMIT 1
    FOR UPDATE SKIP LOCKED)
RETURNING id
"""


def worker_main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int)
    ap.add_argument("--name")
    ap.add_argument("--fire-file")
    ap.add_argument("--stats-file")
    ap.add_argument("--max-seconds", type=float, default=300)
    a = ap.parse_args(argv)
    st = {"worker": a.name, "claims": 0, "fired": 0, "done": 0, "retries_40001": 0, "errors": {},
          "lost_lease_on_done": 0, "error_samples": []}
    deadline = time.monotonic() + a.max_seconds
    conn = None
    fire = open(a.fire_file, "a", buffering=1)

    def note(e):
        k = err_code(e)
        st["errors"][k] = st["errors"].get(k, 0) + 1
        if len(st["error_samples"]) < 8:
            st["error_samples"].append(f"{time.strftime('%H:%M:%S')} {k}: {str(e)[:200]}")

    def ensure():
        nonlocal conn
        if conn is None or conn.closed:
            conn = psycopg.connect(dsn(a.port, timeout=5), autocommit=True)
            conn.execute("SET statement_timeout = '5s'")
        return conn

    st["slow_or_failed_calls"] = []  # DB calls that took > 1 s or failed, with wall-clock times

    def ev(op, t_s, ok, info=""):
        d = time.monotonic() - t_s
        if (d > 1.0 or not ok) and len(st["slow_or_failed_calls"]) < 60:
            st["slow_or_failed_calls"].append(
                f"{time.strftime('%H:%M:%S')} {op} {'ok' if ok else 'ERR'} {d:.1f}s {info}"[:220])

    def txn(fn, op):
        """Run fn(conn) in an explicit SERIALIZABLE transaction; retry on 40001."""
        while True:
            t_s = time.monotonic()
            c = ensure()
            try:
                c.execute("BEGIN")
                r = fn(c)
                c.execute("COMMIT")
                ev(op, t_s, True)
                return r
            except psycopg.Error as e:
                ev(op, t_s, False, f"{err_code(e)} {str(e).splitlines()[0][:120]}")
                t_r = time.monotonic()
                try:
                    c.execute("ROLLBACK")
                except Exception as e2:  # noqa: BLE001
                    ev("rollback", t_r, False, err_code(e2))
                else:
                    ev("rollback", t_r, True)
                if err_code(e) == "40001":
                    st["retries_40001"] += 1
                    time.sleep(0.005)
                    continue
                raise

    while time.monotonic() < deadline:
        try:
            jid = txn(lambda c: q1(c, CLAIM_SQL, {"w": a.name}), "claim")
        except Exception as e:  # noqa: BLE001
            note(e)
            conn = None if (conn is None or conn.closed or isinstance(e, psycopg.OperationalError)) else conn
            time.sleep(0.5)
            continue
        if jid is None:
            try:
                t_s = time.monotonic()
                left = q1(ensure(), "SELECT count(*) FROM jobs WHERE done_at IS NULL")
                ev("count_left", t_s, True)
            except Exception as e:  # noqa: BLE001
                note(e)
                time.sleep(0.5)
                continue
            if left == 0:
                break
            time.sleep(0.2)
            continue
        st["claims"] += 1
        # fire: side effect outside the database
        fire.write(f"{jid},{a.name}\n")
        fire.flush()
        os.fsync(fire.fileno())
        st["fired"] += 1
        time.sleep(0.02)
        while time.monotonic() < deadline:  # mark done; keep trying while the lease may still be ours
            try:
                n = txn(lambda c: c.execute("UPDATE jobs SET done_at = now() WHERE id=%s AND lease_owner=%s "
                                            "AND done_at IS NULL", (jid, a.name)).rowcount, "mark_done")
                if n == 1:
                    st["done"] += 1
                else:
                    st["lost_lease_on_done"] += 1
                break
            except Exception as e:  # noqa: BLE001
                note(e)
                time.sleep(0.5)
    st["exit"] = "timeout" if time.monotonic() >= deadline else "no jobs left"
    Path(a.stats_file).write_text(json.dumps(st))


def s3_run(res, label, do_partition):
    with connect("node1") as c:
        c.execute("TRUNCATE jobs")
        with c.cursor() as cur:
            cur.executemany("INSERT INTO jobs (id, kind, run_at) VALUES (%s, 'reminder', now())",
                            [(uuid.uuid4(),) for _ in range(200)])
        ids = {str(r[0]) for r in c.execute("SELECT id FROM jobs")}
        ranges = c.execute("SELECT range_id, lease_holder, replicas FROM [SHOW RANGES FROM TABLE jobs WITH DETAILS]"
                           ).fetchall()
    out = {"jobs_table_ranges(range_id, leaseholder_node_id, replicas)": [list(r) for r in ranges]}
    procs = []
    files = {}
    for w, node in (("w1", "node1"), ("w2", "node2")):
        files[w] = (OUT / f"s3{label}_{w}_fired.csv", OUT / f"s3{label}_{w}_stats.json")
        files[w][0].write_text("")
        procs.append(subprocess.Popen([sys.executable, __file__, "worker", "--port", str(PORTS[node]),
                                       "--name", w, "--fire-file", str(files[w][0]),
                                       "--stats-file", str(files[w][1]), "--max-seconds", "300"]))
    t0 = time.monotonic()

    def fired_count():
        return sum(len(f.read_text().splitlines()) for f, _ in files.values())

    if do_partition:
        while fired_count() < 100 and time.monotonic() - t0 < 120:
            time.sleep(0.01)
        out["fired_when_partitioned"] = fired_count()
        partition("node1")
        out["partitioned_at_s"] = round(time.monotonic() - t0, 2)
        out["partitioned_at_wall"] = time.strftime("%H:%M:%S")
        log("S3b partitioned node1 at", out["fired_when_partitioned"], "fired")
        time.sleep(60)
        out["fired_during_partition_window_end"] = fired_count()
        heal("node1")
        out["healed_at_s"] = round(time.monotonic() - t0, 2)
        out["healed_at_wall"] = time.strftime("%H:%M:%S")
        log("S3b healed")
    for p in procs:
        p.wait(timeout=400)
    out["wall_seconds"] = round(time.monotonic() - t0, 1)
    counts: dict[str, int] = {}
    by_worker: dict[str, list] = {}
    for w, (f, sf) in files.items():
        for line in f.read_text().splitlines():
            jid, wn = line.split(",")
            counts[jid] = counts.get(jid, 0) + 1
            by_worker.setdefault(jid, []).append(wn)
        out[f"stats_{w}"] = json.loads(sf.read_text()) if sf.exists() else "missing"
    out["fired_exactly_once"] = sum(1 for j in ids if counts.get(j) == 1)
    out["fired_more_than_once"] = sum(1 for j in ids if counts.get(j, 0) > 1)
    out["never_fired"] = sum(1 for j in ids if counts.get(j, 0) == 0)
    out["duplicates_detail"] = {j: by_worker[j] for j in ids if counts.get(j, 0) > 1}
    with connect("node2") as c:
        out["db_done"] = q1(c, "SELECT count(*) FROM jobs WHERE done_at IS NOT NULL")
    res[label] = out
    log("S3", label, {k: v for k, v in out.items() if not k.startswith("stats") and k != "duplicates_detail"})
    for w in ("w1", "w2"):
        log("  ", out[f"stats_{w}"])


@scenario("S3")
def s3(res):
    res["claim_sql"] = CLAIM_SQL.strip()
    s3_run(res, "a_healthy", False)
    s3_run(res, "b_partition_node1", True)
    # make sure node1 rejoined properly
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120:
        if all(v == 0 for v in ranges_underreplicated().values()):
            break
        time.sleep(2)
    res["after_heal_underreplicated_zero_seconds"] = round(time.monotonic() - t0, 1)


# ----------------------------------------------------------------- S4
def vec_lit(v) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


@scenario("S4")
def s4(res):
    rng = np.random.default_rng(42)
    data = rng.standard_normal((20_000, 384)).astype(np.float32)
    data /= np.linalg.norm(data, axis=1, keepdims=True)
    qrng = np.random.default_rng(7)
    queries = qrng.standard_normal((100, 384)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    lits = [vec_lit(v) for v in data]
    batch = 100
    with connect("node1") as c:
        res["index_exists_before_insert"] = q1(
            c, "SELECT count(*) FROM [SHOW INDEXES FROM embeddings] WHERE index_name='embeddings_vec_idx'") > 0
        t0 = time.monotonic()
        with c.cursor() as cur:
            for s in range(0, len(lits), batch):
                rows = [(uuid.uuid4(), uuid.uuid4(), s + k, lits[s + k]) for k in range(min(batch, len(lits) - s))]
                ph = ",".join(["(%s,%s,%s,%s::VECTOR)"] * len(rows))
                cur.execute(f"INSERT INTO embeddings (id, entity_id, chunk, vec) VALUES {ph}",
                            [x for r in rows for x in r])
        res["insert_20000_with_index_seconds"] = round(time.monotonic() - t0, 1)
        res["insert_batch_rows"] = batch
        log("S4 inserted", res["insert_20000_with_index_seconds"], "s")

    # exact ground truth (L2 on float32 data, computed in float64)
    d64, q64 = data.astype(np.float64), queries.astype(np.float64)
    dists = (q64 ** 2).sum(1)[:, None] - 2 * q64 @ d64.T + (d64 ** 2).sum(1)[None, :]
    truth = np.argsort(dists, axis=1)[:, :10]

    qsql = "SELECT chunk FROM embeddings ORDER BY vec <-> %s::VECTOR LIMIT 10"
    with connect("node2") as c:
        plan = [r[0] for r in c.execute("EXPLAIN " + qsql, (vec_lit(queries[0]),))]
        res["node2_plan"] = plan
        res["node2_uses_vector_index"] = any("vector search" in p for p in plan)
        res["node2_row_count"] = q1(c, "SELECT count(*) FROM embeddings")
        res["beam"] = {}
        for beam in (32, 64, 128, 256, 512):  # 32 is the default vector_search_beam_size
            c.execute(f"SET vector_search_beam_size = {beam}")
            for q in queries[:5]:  # warm-up
                c.execute(qsql, (vec_lit(q),)).fetchall()
            lat, hits = [], 0
            for i, q in enumerate(queries):
                s = time.perf_counter()
                got = [r[0] for r in c.execute(qsql, (vec_lit(q),)).fetchall()]
                lat.append((time.perf_counter() - s) * 1000)
                hits += len(set(got) & set(truth[i].tolist()))
            res["beam"][beam] = {"recall_at_10": hits / 1000, "p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95)}
            log("S4 beam", beam, res["beam"][beam])
        c.execute("RESET vector_search_beam_size")
        # exact scan for comparison (no index)
        lat = []
        for q in queries[:20]:
            s = time.perf_counter()
            c.execute("SELECT chunk FROM embeddings@embeddings_pkey ORDER BY vec <-> %s::VECTOR LIMIT 10",
                      (vec_lit(q),)).fetchall()
            lat.append((time.perf_counter() - s) * 1000)
        res["exact_scan_20q"] = {"p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95)}
    # extra: build a vector index on an already populated copy (bulk build time)
    with connect("node1") as c:
        c.execute("CREATE TABLE emb_build AS SELECT id, chunk, vec FROM embeddings")
        c.execute("SET sql_safe_updates = false")
        t0 = time.monotonic()
        try:
            c.execute("CREATE VECTOR INDEX emb_build_idx ON emb_build (vec)")
            res["index_build_on_populated_20000_seconds"] = round(time.monotonic() - t0, 1)
        except psycopg.Error as e:
            res["index_build_on_populated_error"] = str(e)[:300]
        c.execute("DROP TABLE emb_build")


# ----------------------------------------------------------------- S2b
@scenario("S2b")
def s2b(res):
    pre = connect("node1", stmt_timeout="5s")
    ref = pre.execute(checksum_sql()).fetchone()
    probe_id = q1(pre, "SELECT id FROM tasks ORDER BY id LIMIT 1")
    res["tasks_count_before_s2b"] = ref[0]
    res["stop_node2_node3_seconds"] = stop("node2", "node3")
    t_stop = time.monotonic()
    attempts = []
    k = 0
    while time.monotonic() - t_stop < 90:
        a = {"t": round(time.monotonic() - t_stop, 1)}
        title = f"s2b-{k}"
        k += 1
        for label, sql, params in (
            ("insert", "INSERT INTO tasks (id, owner, title, status, updated_at) "
                       "VALUES (gen_random_uuid(), 'me', %s, 'open', now())", (title,)),
            ("select_count", "SELECT count(*) FROM tasks", None),
            ("stale_point_read_local", "SELECT title FROM tasks AS OF SYSTEM TIME "
                                       "with_max_staleness('1h', true) WHERE id = %s", (probe_id,)),
            ("follower_read_timestamp_count", "SELECT count(*) FROM tasks AS OF SYSTEM TIME "
                                              "follower_read_timestamp()", None),
        ):
            s = time.monotonic()
            try:
                if pre.closed:
                    raise psycopg.OperationalError("connection closed")
                r = pre.execute(sql, params)
                a[label] = f"ok ({r.fetchone()[0] if r.description else ''}) in {time.monotonic() - s:.2f}s"
            except psycopg.Error as e:
                a[label] = f"{err_code(e)} after {time.monotonic() - s:.1f}s: {str(e).splitlines()[0][:140]}"
                if pre.closed:
                    try:
                        pre = connect("node1", stmt_timeout="5s")
                    except Exception as e2:  # noqa: BLE001
                        a["reconnect"] = f"{err_code(e2)}: {str(e2).splitlines()[0][:140]}"
            if label == "insert":
                a["insert_title"] = title
        s = time.monotonic()
        try:
            with connect("node1", timeout=5, stmt_timeout="5s") as c2:
                c2.execute("SELECT 1")
                a["new_connection"] = f"ok in {time.monotonic() - s:.2f}s"
        except Exception as e:  # noqa: BLE001
            a["new_connection"] = f"{err_code(e)} after {time.monotonic() - s:.1f}s: {str(e).splitlines()[0][:140]}"
        attempts.append(a)
        log("S2b", a)
    res["attempts_with_only_node1_up"] = attempts
    res["seconds_with_only_node1_up"] = round(time.monotonic() - t_stop, 1)
    res["node1_insert_acks_while_2_down"] = sum(1 for a in attempts if str(a.get("insert", "")).startswith("ok"))
    # bring node2 back: majority restored
    t0 = time.monotonic()
    start("node2")
    t_first = None
    errs = []
    while time.monotonic() - t0 < 300:
        try:
            with connect("node1", timeout=5, stmt_timeout="5s") as c:
                c.execute("INSERT INTO tasks (id, owner, title, status, updated_at) "
                          "VALUES (gen_random_uuid(), 'me', 's2b-recovered', 'open', now())")
                t_first = time.monotonic() - t0
                break
        except Exception as e:  # noqa: BLE001
            errs.append(f"{err_code(e)}: {str(e).splitlines()[0][:120]}")
            time.sleep(0.5)
    res["first_write_on_node1_after_starting_node2_seconds"] = round(t_first, 2) if t_first else None
    res["errors_before_recovery"] = errs[:3] + ([f"... {len(errs)} total"] if len(errs) > 3 else [])
    start("node3")
    wait_sql("node3", "titan")
    with connect("node3") as c:
        res["tasks_count_after_recovery"] = q1(c, "SELECT count(*) FROM tasks")
        res["timed_out_inserts_that_committed_anyway"] = [
            r[0] for r in c.execute("SELECT title FROM tasks WHERE title LIKE 's2b-%%' "
                                    "AND title <> 's2b-recovered' ORDER BY title")]
    t0 = time.monotonic()
    while time.monotonic() - t0 < 180:
        if all(v == 0 for v in ranges_underreplicated().values()):
            break
        time.sleep(2)
    res["underreplicated_zero_after_all_up_seconds"] = round(time.monotonic() - t0, 1)
    try:
        pre.close()
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------- S6
@scenario("S6")
def s6(res):
    time.sleep(60)  # let the cluster go idle
    samples = []
    for _ in range(5):
        p = sh("docker", "stats", "--no-stream", "--format", "{{json .}}",
               *[container(n) for n in PORTS])
        samples.append([json.loads(line) for line in p.stdout.splitlines()])
        time.sleep(5)
    per = {}
    for n in PORTS:
        cid = container(n)[:12]
        rows = [r for s in samples for r in s if r["ID"].startswith(cid) or r["Container"].startswith(cid)]
        per[n] = {"mem_usage": [r["MemUsage"] for r in rows], "cpu": [r["CPUPerc"] for r in rows]}
        du = compose("exec", "-T", n, "du", "-sb", "/cockroach/cockroach-data",
                     "/cockroach/cockroach-data/auxiliary/EMERGENCY_BALLAST", "/cockroach/cockroach-data/logs",
                     check=False).stdout.split()
        per[n]["data_dir_bytes_total"] = int(du[0]) if du else None
        per[n]["emergency_ballast_bytes"] = int(du[2]) if len(du) > 2 else None
        per[n]["logs_bytes"] = int(du[4]) if len(du) > 4 else None
        if len(du) > 4:
            per[n]["data_excl_ballast_and_logs_bytes"] = int(du[0]) - int(du[2]) - int(du[4])
    res["per_node"] = per
    res["image"] = IMAGE
    res["image_size_bytes"] = int(sh("docker", "image", "inspect", "-f", "{{.Size}}", IMAGE).stdout.strip())
    with connect("node1") as c:
        res["row_counts"] = {t: q1(c, f"SELECT count(*) FROM {t}") for t in ("tasks", "jobs", "embeddings")}
    log("S6", json.dumps(per))


# ----------------------------------------------------------------- S7
@scenario("S7")
def s7(res):
    n1 = R["node_ids"]["node1"]
    with connect("node1", stmt_timeout="0") as c:
        counts = {t: q1(c, f"SELECT count(*) FROM {t}") for t in ("tasks", "jobs", "embeddings", "alembic_version")}
        ref_md5 = c.execute(checksum_sql()).fetchone()
        t0 = time.monotonic()
        rows = c.execute(f"BACKUP DATABASE titan INTO 'nodelocal://{n1}/titan-backup'").fetchall()
        res["backup_seconds"] = round(time.monotonic() - t0, 1)
        res["backup_result"] = [list(map(str, r)) for r in rows]
    res["backup_dir_bytes"] = compose("exec", "-T", "node1", "du", "-sb", "/backup", check=False).stdout.strip()
    compose("up", "-d", "restore")
    wait_sql(RESTORE_PORT, "defaultdb")
    with connect(RESTORE_PORT, "defaultdb") as c:
        t0 = time.monotonic()
        c.execute("RESTORE DATABASE titan FROM LATEST IN 'nodelocal://1/titan-backup'")
        res["restore_seconds"] = round(time.monotonic() - t0, 1)
    with connect(RESTORE_PORT, "titan") as c:
        rcounts = {t: q1(c, f"SELECT count(*) FROM {t}") for t in ("tasks", "jobs", "embeddings", "alembic_version")}
        rmd5 = c.execute(checksum_sql()).fetchone()
        res["restored_alembic_version"] = q1(c, "SELECT version_num FROM alembic_version")
        try:
            c.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
            plan = [r[0] for r in c.execute("EXPLAIN SELECT chunk FROM embeddings ORDER BY vec <-> "
                                            "(SELECT vec FROM embeddings LIMIT 1) LIMIT 10")]
            res["restored_vector_index_used"] = any("vector search" in p for p in plan)
        except psycopg.Error as e:
            res["restored_vector_query_error"] = str(e)[:300]
    res["source_counts"] = counts
    res["restored_counts"] = rcounts
    res["counts_match"] = counts == rcounts
    res["tasks_checksum_match"] = tuple(ref_md5) == tuple(rmd5)
    log("S7", res)


# ----------------------------------------------------------------- misc
def licence_evidence():
    logs = compose("exec", "-T", "node1", "sh", "-c",
                   "cat /cockroach/cockroach-data/logs/cockroach.log /cockroach/cockroach-data/logs/*.log 2>/dev/null",
                   check=False).stdout
    R["licence_log_lines"] = [ln[:300] for ln in logs.splitlines()
                              if "licens" in ln.lower() or "throttl" in ln.lower() or "telemetry" in ln.lower()][:15]
    try:
        with connect("node1", "defaultdb") as c:
            try:
                c.execute("SET CLUSTER SETTING diagnostics.reporting.enabled = false")
                R["disable_telemetry_without_license"] = "accepted (SET CLUSTER SETTING succeeded)"
                c.execute("SET CLUSTER SETTING diagnostics.reporting.enabled = true")
            except psycopg.Error as e:
                R["disable_telemetry_without_license"] = f"rejected: {e}"
    except Exception as e:  # noqa: BLE001
        R["disable_telemetry_without_license"] = f"not tested: {e}"


def main():
    t0 = time.monotonic()
    R["packages"] = {m: __import__("importlib.metadata").metadata.version(m) for m in
                     ("sqlalchemy", "sqlalchemy-cockroachdb", "alembic", "psycopg", "pgvector", "numpy")}
    R["image"] = IMAGE
    cluster_up()
    s5()
    s1()
    s2()
    s3()
    s4()
    s6()
    s2b()
    s7()
    licence_evidence()
    R["total_seconds"] = round(time.monotonic() - t0, 1)
    save()
    if os.environ.get("KEEP") != "1":
        log("teardown: compose down -v")
        compose("down", "-v", "--remove-orphans", check=False)
    log("done in", R["total_seconds"], "s")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker_main(sys.argv[2:])
    else:
        main()
