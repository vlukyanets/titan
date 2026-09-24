"""ADR 0006 spike harness for YugabyteDB (YSQL) + pgvector (ybhnsw).

Throwaway code. `python harness.py all` runs every scenario from a clean
cluster and writes results.json. `python harness.py worker ...` is the S3 job
worker (started as a separate process by the S3 scenario).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import Counter
from pathlib import Path

import numpy as np
import psycopg
from psycopg import errors as pgerr

HERE = Path(__file__).resolve().parent
PROJECT = "spike-yugabyte"
IMAGE = "yugabytedb/yugabyte:2026.1.2.0-b137"
PORTS = {"node1": 16301, "node2": 16302, "node3": 16303, "restore": 16304, "ddlprobe": 16305}
CLUSTER_NET = f"{PROJECT}_cluster"
OUT = HERE / "out"
RESULTS: dict = {}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def container(node: str) -> str:
    return f"{PROJECT}-{node}-1"


def sh(*args, check=True, capture=True, timeout=None, input=None):
    r = subprocess.run(list(args), capture_output=capture, text=True, timeout=timeout, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"{args} failed rc={r.returncode}: {r.stdout}\n{r.stderr}")
    return r


def compose(*args, **kw):
    return sh("docker", "compose", "-p", PROJECT, "-f", str(HERE / "docker-compose.yml"), *args, **kw)


def connect(node: str, autocommit=True, stmt_timeout_ms=30000, connect_timeout=10):
    conn = psycopg.connect(
        host="127.0.0.1", port=PORTS[node], user="yugabyte", password="yugabyte",
        dbname="yugabyte", autocommit=autocommit, connect_timeout=connect_timeout,
    )
    if stmt_timeout_ms:
        with conn.cursor() as c:
            c.execute(f"SET statement_timeout = {int(stmt_timeout_ms)}")
        if not autocommit:
            conn.commit()
    return conn


def pct(values, p):
    if not values:
        return None
    return float(np.percentile(np.array(values), p))


def ms(x):
    return None if x is None else round(x * 1000, 2)


def is_retryable(e: Exception) -> bool:
    return isinstance(e, (pgerr.SerializationFailure, pgerr.DeadlockDetected)) or getattr(
        e, "sqlstate", None) in ("40001", "40P01")


def err_str(e: Exception) -> str:
    s = f"{type(e).__name__}: {str(e).strip()}"
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", s)
    s = re.sub(r"[0-9a-f]{32}", "<id>", s)
    s = re.sub(r"\d+(\.\d+)?(ms|s)\b", "<t>", s)
    return s.splitlines()[0][:220]


# ---------------------------------------------------------------- cluster ops
def wait_ysql(node: str, timeout=300) -> float:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            with connect(node, connect_timeout=5) as c:
                c.execute("select 1")
            return time.time() - t0
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    raise TimeoutError(f"{node} YSQL not ready after {timeout}s: {last}")


def master_health(via: str = "node1") -> dict:
    code = (
        "import urllib.request,json,sys\n"
        "for h in ['node1-cluster','node2-cluster','node3-cluster']:\n"
        "    try:\n"
        "        r=urllib.request.urlopen('http://%s:7000/api/v1/health-check'%h,timeout=5).read().decode()\n"
        "        d=json.loads(r)\n"
        "        if 'dead_nodes' in d: print(r); sys.exit(0)\n"
        "    except Exception as e: pass\n"
        "print('{}')\n"
    )
    r = sh("docker", "exec", container(via), "python3", "-c", code, check=False, timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {}


def wait_fully_replicated(via="node1", timeout=600) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = master_health(via)
        if h and not h.get("dead_nodes") and not h.get("under_replicated_tablets"):
            return time.time() - t0
        time.sleep(2)
    raise TimeoutError(f"cluster still under-replicated after {timeout}s: {master_health(via)}")


def yb_admin(*args, via="node1"):
    return sh("docker", "exec", container(via), "bin/yb-admin", "--master_addresses",
              "node1-cluster:7100,node2-cluster:7100,node3-cluster:7100", *args, check=False, timeout=120)


def cluster_topology() -> dict:
    masters = yb_admin("list_all_masters").stdout
    tservers = yb_admin("list_all_tablet_servers").stdout
    cfg = yb_admin("get_universe_config").stdout
    rf = None
    try:
        rf = json.loads(cfg.strip().splitlines()[-1])["replicationInfo"]["liveReplicas"]["numReplicas"]
    except Exception:  # noqa: BLE001
        pass
    return {"masters": masters.strip().splitlines(), "tservers_alive": tservers.count("ALIVE"),
            "replication_factor": rf}


def setup_cluster():
    log("tearing down any previous spike-yugabyte state")
    compose("--profile", "restore", "--profile", "ddlprobe", "down", "-v", "--remove-orphans", check=False)
    t0 = time.time()
    compose("up", "-d", "node1", "node2", "node3", timeout=900)
    for n in ("node1", "node2", "node3"):
        wait_ysql(n)
    t_ysql = time.time() - t0
    wait_fully_replicated()
    t_rep = time.time() - t0
    fix = fix_tserver_master_lists()
    topo = cluster_topology()
    log("cluster up", topo)
    with connect("node1") as c:
        ver = c.execute("select version()").fetchone()[0]
        iso = c.execute("show transaction_isolation").fetchone()[0]
    RESULTS["setup"] = {"seconds_to_ysql_ready": round(t_ysql, 1),
                        "seconds_to_fully_replicated": round(t_rep, 1),
                        "server_version": ver, "default_transaction_isolation": iso,
                        "tserver_master_list_fix": fix, **topo}


def tserver_client_masters(node: str) -> dict:
    """Master list the running tserver actually uses (cmdline flag + last client log line)."""
    cmd = sh("docker", "exec", container(node), "bash", "-c",
             "ps -o args= -C yb-tserver | tr ' ' '\\n' | grep -- --tserver_master_addrs= | cut -d= -f2",
             check=False).stdout.strip()
    logl = sh("docker", "exec", container(node), "bash", "-c",
              "grep -h 'New master addresses' /home/yugabyte/ybd/data/yb-data/tserver/logs/yb-tserver.INFO "
              "| tail -1 | sed 's/.*New master addresses: //'", check=False).stdout.strip()
    return {"cmdline": cmd, "client_log": logl}


def fix_tserver_master_lists() -> dict:
    """yugabyted starts each tserver with only the masters that existed when it joined
    (node1: itself; node2: node1+node2). Its 60 s poller later rewrites the gflag and
    yugabyted.conf, but the running tserver's client keeps the old list, so a master
    failover to a master it does not know about makes that node unusable (observed in a
    first S3b run). Work-around: once yugabyted.conf lists all three masters on every
    node, restart each node whose tserver was started with fewer than three."""
    out = {"before": {n: tserver_client_masters(n) for n in ("node1", "node2", "node3")}}
    t0 = time.time()
    for n in ("node1", "node2", "node3"):
        while time.time() - t0 < 180:
            conf = sh("docker", "exec", container(n), "grep", "current_masters",
                      "/home/yugabyte/ybd/conf/yugabyted.conf", check=False).stdout
            if conf.count("-cluster:7100") == 3:
                break
            time.sleep(2)
    out["seconds_until_conf_lists_3_masters"] = round(time.time() - t0, 1)
    restarted = []
    for n in ("node1", "node2", "node3"):
        if out["before"][n]["cmdline"].count("-cluster:7100") < 3:
            compose("restart", n, timeout=300)
            wait_ysql(n)
            restarted.append(n)
    wait_fully_replicated()
    out["restarted"] = restarted
    out["seconds_total"] = round(time.time() - t0, 1)
    out["after"] = {n: tserver_client_masters(n) for n in ("node1", "node2", "node3")}
    log("master-list fix", out)
    return out


def stop(*nodes):
    compose("stop", *nodes, timeout=300)


def start(*nodes):
    # plain `docker start`: `compose start` would block on depends_on health checks
    sh("docker", "start", *[container(n) for n in nodes], timeout=300)


def partition(node):
    sh("docker", "network", "disconnect", CLUSTER_NET, container(node))


def heal(node):
    sh("docker", "network", "connect", "--alias", f"{node}-cluster", CLUSTER_NET, container(node))


# ---------------------------------------------------------------- alembic
def alembic(target: str, node: str) -> dict:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(HERE / "alembic.ini"))
    cfg.set_main_option("script_location", str(HERE / "migrations"))
    cfg.set_main_option("sqlalchemy.url",
                        f"postgresql+psycopg://yugabyte:yugabyte@127.0.0.1:{PORTS[node]}/yugabyte")
    cfg.attributes["skip_logging"] = True
    t0 = time.time()
    try:
        command.upgrade(cfg, target)
        return {"ok": True, "seconds": round(time.time() - t0, 2)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "seconds": round(time.time() - t0, 2), "error": err_str(e),
                "trace": traceback.format_exc()[-1500:]}


def schema_state(node: str) -> dict:
    with connect(node) as c:
        tables = [r[0] for r in c.execute(
            "select table_name from information_schema.tables where table_schema='public' order by 1")]
        cols = [r[0] for r in c.execute(
            "select column_name from information_schema.columns where table_name='tasks' order by ordinal_position")]
        idx = c.execute(
            "select i.relname, am.amname, i.reloptions from pg_class i join pg_am am on am.oid=i.relam "
            "where i.relname='embeddings_vec_ybhnsw'").fetchall()
        ext = c.execute("select extversion from pg_extension where extname='vector'").fetchone()
        try:
            ver = [r[0] for r in c.execute("select version_num from alembic_version")]
        except Exception:  # noqa: BLE001
            ver = None
    return {"tables": tables, "tasks_columns": cols,
            "vector_index": [list(map(str, r)) for r in idx],
            "vector_extension": ext[0] if ext else None, "alembic_version": ver}


def ddl_rollback_probe(node: str) -> dict:
    """Does a DDL inside BEGIN ... ROLLBACK get rolled back (what Alembic assumes)?"""
    out = {}
    with connect(node) as c:
        c.execute("drop table if exists ddl_probe")
        c.execute("begin")
        c.execute("create table ddl_probe(x int)")
        c.execute("insert into ddl_probe values (1)")
        c.execute("rollback")
        exists = c.execute("select to_regclass('ddl_probe')").fetchone()[0]
        out["create_table_survives_rollback"] = exists is not None
        if exists:
            out["rows_in_probe_after_rollback"] = c.execute("select count(*) from ddl_probe").fetchone()[0]
            c.execute("drop table ddl_probe")
        c.execute("begin")
        c.execute("create index probe_idx on tasks(owner)")
        notices = []
        c.execute("rollback")
        out["create_index_survives_rollback"] = c.execute(
            "select to_regclass('probe_idx')").fetchone()[0] is not None
        if out["create_index_survives_rollback"]:
            c.execute("drop index probe_idx")
        out["notices"] = notices
    return out


def s5_alembic():
    log("S5 alembic")
    r = {}
    r["upgrade_0001_on_node1"] = alembic("0001_initial", "node1")
    t0 = time.time()
    seen = {}
    while time.time() - t0 < 60 and len(seen) < 2:
        for n in ("node2", "node3"):
            if n in seen:
                continue
            st = schema_state(n)
            if {"tasks", "jobs", "embeddings"} <= set(st["tables"]) and st["alembic_version"] == ["0001_initial"]:
                seen[n] = {"seconds_until_visible": round(time.time() - t0, 3), **st}
        time.sleep(0.2)
    r["after_0001"] = seen
    r["ddl_rollback_probe_default_flags"] = ddl_rollback_probe("node1")

    stop("node3")
    t_stop = time.time()
    r["upgrade_head_on_node1_with_node3_stopped"] = alembic("head", "node1")
    r["upgrade_head_started_seconds_after_node3_stop"] = round(time.time() - t_stop, 1)
    r["node1_after_head"] = schema_state("node1")
    t0 = time.time()
    start("node3")
    r["node3_ysql_ready_seconds"] = round(wait_ysql("node3") + 0, 1)
    r["node3_after_restart"] = schema_state("node3")
    r["node3_schema_check_seconds_after_start"] = round(time.time() - t0, 1)
    r["seconds_until_fully_replicated_after_node3_start"] = round(wait_fully_replicated(), 1)
    RESULTS["S5"] = r
    log("S5", json.dumps({k: v for k, v in r.items() if k != "after_0001"}, default=str)[:2000])


def s5_extra_transactional_ddl():
    """Extra: single node with the early-access transactional DDL flag enabled."""
    log("S5 extra: transactional DDL flag")
    r = {}
    try:
        compose("--profile", "ddlprobe", "up", "-d", "ddlprobe", timeout=600)
        wait_ysql("ddlprobe", timeout=300)
        r["probe"] = ddl_rollback_probe_nolock("ddlprobe")
        r["alembic_head"] = alembic("head", "ddlprobe")
        r["schema"] = schema_state("ddlprobe")
    except Exception as e:  # noqa: BLE001
        r["error"] = err_str(e)
    finally:
        compose("--profile", "ddlprobe", "rm", "-s", "-f", "-v", "ddlprobe", check=False)
    RESULTS["S5_extra_transactional_ddl"] = r
    log("S5 extra", r)


def ddl_rollback_probe_nolock(node):
    out = {}
    with connect(node) as c:
        c.execute("begin")
        c.execute("create table ddl_probe(x int)")
        c.execute("insert into ddl_probe values (1)")
        c.execute("rollback")
        out["create_table_survives_rollback"] = c.execute(
            "select to_regclass('ddl_probe')").fetchone()[0] is not None
    return out


# ---------------------------------------------------------------- S1
def s1_trial(isolation: str, trial: int) -> dict:
    tid = uuid.uuid4()
    with connect("node1") as c:
        c.execute("insert into tasks(id, owner, title, status, updated_at) values (%s,'s1','orig','open',now())",
                  (tid,))
    t0 = time.time()
    while True:
        ok = 0
        for n in ("node1", "node2", "node3"):
            with connect(n) as c:
                ok += c.execute("select count(*) from tasks where id=%s", (tid,)).fetchone()[0]
        if ok == 3:
            break
    visible_all = time.time() - t0
    conns = {n: connect(n) for n in ("node1", "node2")}
    barrier = threading.Barrier(2)
    res = {}

    def work(node):
        conn = conns[node]
        val = f"from-{node}-{trial}"
        attempts, errors = 0, []
        barrier.wait()
        t = time.time()
        while True:
            attempts += 1
            try:
                if isolation == "read committed":
                    cur = conn.execute("update tasks set title=%s, version=version+1, updated_at=now() where id=%s",
                                       (val, tid))
                    rc = cur.rowcount
                else:
                    conn.execute(f"begin isolation level {isolation}")
                    v = conn.execute("select version from tasks where id=%s", (tid,)).fetchone()[0]
                    cur = conn.execute("update tasks set title=%s, version=%s, updated_at=now() where id=%s",
                                       (val, v + 1, tid))
                    rc = cur.rowcount
                    conn.execute("commit")
                break
            except Exception as e:  # noqa: BLE001
                errors.append(err_str(e))
                try:
                    conn.execute("rollback")
                except Exception:  # noqa: BLE001
                    pass
                if not is_retryable(e) or attempts > 20:
                    rc = None
                    break
        res[node] = {"value": val, "attempts": attempts, "retries": attempts - 1, "errors": errors,
                     "rowcount": rc, "latency_ms": ms(time.time() - t), "finished_at": time.time()}

    th = [threading.Thread(target=work, args=(n,)) for n in ("node1", "node2")]
    for t in th:
        t.start()
    for t in th:
        t.join()
    t_done = time.time()
    for c in conns.values():
        c.close()
    # convergence
    while True:
        vals = {}
        for n in ("node1", "node2", "node3"):
            with connect(n) as c:
                vals[n] = c.execute("select title, version from tasks where id=%s", (tid,)).fetchone()
        if len({v for v in vals.values()}) == 1:
            break
    conv = time.time() - t_done
    order = sorted(res, key=lambda n: res[n]["finished_at"])
    for v in res.values():
        v.pop("finished_at")
    return {"isolation": isolation, "insert_visible_on_all_nodes_s": round(visible_all, 3),
            "writers": res, "finished_order": order,
            "final": {n: {"title": v[0], "version": v[1]} for n, v in vals.items()},
            "converged_s_after_both_returned": round(conv, 3)}


def s1_conflicts():
    log("S1 conflicting writes")
    trials = [s1_trial("read committed", i) for i in range(5)]
    trials += [s1_trial("repeatable read", 10 + i) for i in range(3)]
    trials += [s1_trial("serializable", 20 + i) for i in range(3)]
    RESULTS["S1"] = {"trials": trials}
    for t in trials:
        log("S1", t["isolation"], {n: (w["retries"], w["errors"][:1], w["rowcount"]) for n, w in t["writers"].items()},
            "final", t["final"]["node3"], "conv", t["converged_s_after_both_returned"])


# ---------------------------------------------------------------- S2 / S2b
CHECKSUM_SQL = "select count(*), md5(coalesce(string_agg(id::text || ':' || title, ',' order by id), '')) from tasks"


def checksum(node):
    with connect(node, stmt_timeout_ms=60000) as c:
        return tuple(c.execute(CHECKSUM_SQL).fetchone())


def insert_many(n_rows: int, nodes: list[str], tag: str, stmt_timeout_ms=30000):
    conns = {n: connect(n, stmt_timeout_ms=stmt_timeout_ms) for n in nodes}
    lat = {n: [] for n in nodes}
    errors = Counter()
    ok_by_node = Counter()
    t0 = time.time()
    for i in range(n_rows):
        node = nodes[i % len(nodes)]
        tid = uuid.uuid4()
        for attempt in range(10):
            t = time.time()
            try:
                conns[node].execute(
                    "insert into tasks(id, owner, title, status, updated_at) values (%s,%s,%s,'open',now())",
                    (tid, tag, f"{tag}-{i}"))
                lat[node].append(time.time() - t)
                ok_by_node[node] += 1
                break
            except Exception as e:  # noqa: BLE001
                errors[err_str(e)] += 1
                if conns[node].closed or conns[node].broken:
                    conns[node] = connect(node, stmt_timeout_ms=stmt_timeout_ms)
                # the insert may have committed (ambiguous); a duplicate key on retry tells us so
                time.sleep(0.2)
    total = time.time() - t0
    for c in conns.values():
        c.close()
    all_lat = [x for v in lat.values() for x in v]
    return {"rows": n_rows, "seconds": round(total, 1), "ok_by_node": dict(ok_by_node),
            "write_errors": sum(errors.values()), "errors": dict(errors),
            "p50_ms": ms(pct(all_lat, 50)), "p95_ms": ms(pct(all_lat, 95)), "p99_ms": ms(pct(all_lat, 99)),
            "max_ms": ms(max(all_lat) if all_lat else None),
            "per_node_p50_ms": {n: ms(pct(v, 50)) for n, v in lat.items()},
            "per_node_p95_ms": {n: ms(pct(v, 95)) for n, v in lat.items()}}


def tserver_tablet_states(node: str) -> dict:
    """Count tablet peers on a tserver by state, from its /api/v1/tablets endpoint (if present)."""
    code = (
        "import urllib.request,json\n"
        f"u='http://{node}-cluster:9000/api/v1/tablets'\n"
        "try:\n"
        "    print(urllib.request.urlopen(u,timeout=5).read().decode())\n"
        "except Exception as e: print(json.dumps({'error':str(e)}))\n"
    )
    r = sh("docker", "exec", container(node), "python3", "-c", code, check=False, timeout=30)
    try:
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return {"raw": r.stdout[:300]}


WATERMARK_SCRIPT = r"""
import urllib.request, json, re, sys
table = sys.argv[1]
d = json.loads(urllib.request.urlopen('http://node1-cluster:9000/api/v1/tablets', timeout=5).read())
out = {}
for tid, v in d.items():
    if v['table_name'] != table:
        continue
    leader = [list(x.values())[0] for x in v['raft_config'] if 'LEADER' in x]
    if not leader:
        out[tid] = {'leader': None}
        continue
    t = urllib.request.urlopen('http://%s:9000/tablet-consensus-status?id=%s' % (leader[0], tid), timeout=5).read().decode()
    peers = {}
    for m in re.finditer(r'Host: ([\w-]+)</li></ul></td><td>\{ peer: \w+ is_new: \d+ last_received: (\d+)\.(\d+) .*?last_applied: (\d+)\.(\d+)', t, re.S):
        peers[m.group(1)] = {'received': int(m.group(3)), 'applied': int(m.group(5))}
    out[tid] = {'leader': leader[0], 'peers': peers}
print(json.dumps(out))
"""


def watermarks(table: str, via="node1") -> dict:
    r = sh("docker", "exec", container(via), "python3", "-c", WATERMARK_SCRIPT, table, check=False, timeout=30)
    try:
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return {"error": (r.stdout + r.stderr)[-300:]}


def replica_caught_up(table: str, node: str) -> tuple[bool, dict]:
    w = watermarks(table)
    if not w or "error" in w:
        return False, w
    for tid, v in w.items():
        peers = v.get("peers") or {}
        me = peers.get(f"{node}-cluster")
        if not me:
            return False, w
        top = max(p["applied"] for p in peers.values())
        if me["applied"] < top or me["received"] < top:
            return False, w
    return True, w


def s2_offline():
    log("S2 node3 offline")
    r = {}
    base = checksum("node1")
    stop("node3")
    t_stop = time.time()
    r["baseline_rows_before"] = base[0]
    r["insert"] = insert_many(10000, ["node1", "node2"], "s2")
    r["insert_started_s_after_stop"] = 0.0
    r["insert_finished_s_after_stop"] = round(time.time() - t_stop, 1)
    r["health_while_node3_down"] = master_health("node1")
    r["health_while_node3_down"]["under_replicated_tablets"] = len(
        r["health_while_node3_down"].get("under_replicated_tablets", []))
    want = checksum("node1")
    r["expected_rows"] = want[0]
    r["expected_md5"] = want[1]
    r["tasks_tablet_watermarks_before_node3_start"] = watermarks("tasks")
    t0 = time.time()
    start("node3")
    r["node3_ysql_ready_s"] = round(wait_ysql("node3"), 2)
    while time.time() - t0 < 600:
        ok, w = replica_caught_up("tasks", "node3")
        if ok:
            break
        time.sleep(0.5)
    r["node3_tasks_replica_caught_up_s_after_start"] = round(time.time() - t0, 2) if ok else None
    r["tasks_tablet_watermarks_at_catch_up"] = w
    while True:
        try:
            got = checksum("node3")
        except Exception as e:  # noqa: BLE001
            got = (None, err_str(e))
        if got == want:
            break
        if time.time() - t0 > 600:
            break
        time.sleep(0.5)
    r["node3_checksum_match_s_after_start"] = round(time.time() - t0, 2)
    r["node3_checksum"] = list(got)
    wait_fully_replicated()
    r["node3_fully_replicated_s_after_start"] = round(time.time() - t0, 2)
    # local-replica read on node3 (follower read), a direct check that its own copy has the rows
    try:
        with connect("node3") as c:
            c.execute("set yb_read_from_followers = true")
            c.execute("set yb_follower_read_staleness_ms = 2000")
            c.execute("set default_transaction_read_only = true")
            time.sleep(2.5)
            r["node3_follower_read_checksum"] = list(c.execute(CHECKSUM_SQL).fetchone())
    except Exception as e:  # noqa: BLE001
        r["node3_follower_read_checksum"] = err_str(e)
    RESULTS["S2"] = r
    log("S2", json.dumps(r, default=str)[:1500])


def s2b_two_down():
    log("S2b two nodes down")
    r = {}
    stop("node2", "node3")
    t_stop = time.time()
    attempts = []
    for i in range(5):
        t = time.time()
        try:
            with connect("node1", stmt_timeout_ms=15000, connect_timeout=20) as c:
                c.execute("insert into tasks(id, owner, title, status, updated_at) values (%s,'s2b',%s,'open',now())",
                          (uuid.uuid4(), f"s2b-{i}"))
            attempts.append({"op": "insert", "ok": True, "s": round(time.time() - t, 2)})
        except Exception as e:  # noqa: BLE001
            attempts.append({"op": "insert", "ok": False, "s": round(time.time() - t, 2), "error": err_str(e)})
    for q in ("select count(*) from tasks", "select 1"):
        t = time.time()
        try:
            with connect("node1", stmt_timeout_ms=15000, connect_timeout=20) as c:
                v = c.execute(q).fetchone()[0]
            attempts.append({"op": q, "ok": True, "s": round(time.time() - t, 2), "value": v})
        except Exception as e:  # noqa: BLE001
            attempts.append({"op": q, "ok": False, "s": round(time.time() - t, 2), "error": err_str(e)})
    # follower read (stale read from node1's own replica)
    t = time.time()
    try:
        with connect("node1", stmt_timeout_ms=15000, connect_timeout=20) as c:
            c.execute("set yb_read_from_followers = true")
            c.execute("set default_transaction_read_only = true")
            v = c.execute("select count(*) from tasks").fetchone()[0]
        attempts.append({"op": "follower read count(*)", "ok": True, "s": round(time.time() - t, 2), "value": v})
    except Exception as e:  # noqa: BLE001
        attempts.append({"op": "follower read count(*)", "ok": False, "s": round(time.time() - t, 2),
                         "error": err_str(e)})
    r["while_two_down"] = attempts
    RESULTS["S2b"] = r
    r["seconds_with_two_down"] = round(time.time() - t_stop, 1)
    # bring back ONE node: does the cluster recover with 2 of 3?
    t0 = time.time()
    start("node2")
    ok_at = None
    errs = Counter()
    while time.time() - t0 < 300:
        try:
            with connect("node1", stmt_timeout_ms=5000, connect_timeout=10) as c:
                c.execute("insert into tasks(id, owner, title, status, updated_at) values (%s,'s2b','back','open',now())",
                          (uuid.uuid4(),))
            ok_at = time.time() - t0
            break
        except Exception as e:  # noqa: BLE001
            errs[err_str(e)] += 1
            time.sleep(1)
    r["after_node2_start_first_successful_write_s"] = None if ok_at is None else round(ok_at, 1)
    r["errors_until_recovered"] = dict(errs)
    start("node3")
    wait_ysql("node3")
    r["node3_back_fully_replicated_s"] = round(wait_fully_replicated(), 1)
    RESULTS["S2b"] = r
    log("S2b", json.dumps(r, default=str)[:2500])


# ---------------------------------------------------------------- S3
CLAIM_SQL = """
update jobs set lease_owner = %(w)s, lease_until = now() + interval '30 seconds'
where id = (
    select id from jobs
    where done_at is null and run_at <= now() and (lease_until is null or lease_until < now())
    order by run_at
    limit 1
    for update skip locked)
returning id
"""


def worker_main(args):
    name, node, out_path, deadline = args.name, args.node, Path(args.out), time.time() + args.max_seconds
    stats = {"worker": name, "node": node, "claimed": 0, "fired": 0, "marked_done": 0,
             "done_update_matched_0_rows": 0, "retries": 0, "errors": Counter(), "idle_polls": 0}
    conn = None
    f = open(out_path, "a", buffering=1)
    while time.time() < deadline:
        try:
            if conn is None or conn.closed or conn.broken:
                conn = connect(node, stmt_timeout_ms=10000, connect_timeout=10)
            # claim (single statement, autocommit), retry on serialization conflicts
            job = None
            for _ in range(50):
                try:
                    row = conn.execute(CLAIM_SQL, {"w": name}).fetchone()
                    job = row[0] if row else None
                    break
                except Exception as e:  # noqa: BLE001
                    if is_retryable(e):
                        stats["retries"] += 1
                        stats["errors"]["retryable: " + err_str(e)] += 1
                        continue
                    raise
            if job is None:
                left = conn.execute("select count(*) from jobs where done_at is null").fetchone()[0]
                if left == 0:
                    break
                stats["idle_polls"] += 1
                time.sleep(0.5)
                continue
            stats["claimed"] += 1
            f.write(f"{job},{name}\n")
            f.flush()
            os.fsync(f.fileno())
            stats["fired"] += 1
            for _ in range(50):
                try:
                    cur = conn.execute(
                        "update jobs set done_at = now() where id = %s and lease_owner = %s and done_at is null",
                        (job, name))
                    if cur.rowcount == 1:
                        stats["marked_done"] += 1
                    else:
                        stats["done_update_matched_0_rows"] += 1
                    break
                except Exception as e:  # noqa: BLE001
                    if is_retryable(e):
                        stats["retries"] += 1
                        stats["errors"]["retryable: " + err_str(e)] += 1
                        continue
                    raise
        except Exception as e:  # noqa: BLE001
            stats["errors"][err_str(e)] += 1
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
            time.sleep(0.5)
    f.close()
    stats["errors"] = dict(stats["errors"])
    stats["timed_out"] = time.time() >= deadline
    Path(str(out_path) + ".stats.json").write_text(json.dumps(stats, indent=1))


def s3_run(label: str, partition_after: int | None):
    with connect("node1") as c:
        c.execute("delete from jobs")
        with c.cursor() as cur:
            cur.executemany("insert into jobs(id, kind, run_at) values (%s, 'reminder', now())",
                            [(uuid.uuid4(),) for _ in range(200)])
        ids = {str(r[0]) for r in c.execute("select id from jobs")}
    OUT.mkdir(exist_ok=True)
    files = {w: OUT / f"s3{label}_{w}.log" for w in ("w1", "w2")}
    for p in files.values():
        p.unlink(missing_ok=True)
        Path(str(p) + ".stats.json").unlink(missing_ok=True)
    procs = []
    t0 = time.time()
    for w, node in (("w1", "node1"), ("w2", "node2")):
        procs.append(subprocess.Popen([sys.executable, str(HERE / "harness.py"), "worker", "--name", w,
                                       "--node", node, "--out", str(files[w]), "--max-seconds", "420"]))
    events = {}
    if partition_after is not None:
        while True:
            n = sum(len(p.read_text().splitlines()) for p in files.values() if p.exists())
            if n >= partition_after:
                break
            if all(p.poll() is not None for p in procs):
                break
            time.sleep(0.02)
        events["fired_at_partition"] = n
        partition("node1")
        tp = time.time()
        events["partitioned_at_s"] = round(tp - t0, 2)
        time.sleep(60)
        events["fired_during_partition_by_worker"] = {
            w: len(p.read_text().splitlines()) if p.exists() else 0 for w, p in files.items()}
        heal("node1")
        events["healed_at_s"] = round(time.time() - t0, 2)
    for p in procs:
        p.wait()
    total = time.time() - t0
    fired = Counter()
    by_worker = Counter()
    for w, p in files.items():
        for line in p.read_text().splitlines():
            jid, wk = line.split(",")
            fired[jid] += 1
            by_worker[wk] += 1
    with connect("node2") as c:
        not_done = c.execute("select count(*) from jobs where done_at is null").fetchone()[0]
    stats = {w: json.loads(Path(str(p) + ".stats.json").read_text()) for w, p in files.items()}
    res = {"jobs": 200, "exactly_once": sum(1 for j in ids if fired[j] == 1),
           "more_than_once": sum(1 for j in ids if fired[j] > 1),
           "never": sum(1 for j in ids if fired[j] == 0),
           "duplicate_job_ids": [j for j in ids if fired[j] > 1],
           "fired_by_worker": dict(by_worker), "jobs_not_marked_done_in_db": not_done,
           "seconds": round(total, 1), "events": events, "worker_stats": stats}
    if partition_after is not None:
        res["node1_rejoined_fully_replicated_s"] = round(wait_fully_replicated(via="node2"), 1)
    return res


def s3_jobs():
    log("S3 jobs a")
    a = s3_run("a", None)
    log("S3a", {k: v for k, v in a.items() if k != "worker_stats"}, a["worker_stats"])
    wait_fully_replicated()
    log("S3 jobs b (partition node1)")
    b = s3_run("b", 100)
    log("S3b", {k: v for k, v in b.items() if k != "worker_stats"}, b["worker_stats"])
    RESULTS["S3"] = {"claim_sql": CLAIM_SQL.strip(), "a_healthy": a, "b_partition": b}


# ---------------------------------------------------------------- S4
def vec_literal(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def s4_vectors():
    log("S4 vectors")
    r = {}
    rng = np.random.default_rng(42)
    X = rng.standard_normal((20000, 384)).astype(np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    ids = [uuid.UUID(int=i + 1) for i in range(len(X))]
    with connect("node1", stmt_timeout_ms=0) as c:
        c.execute("delete from embeddings")
        t0 = time.time()
        with c.cursor() as cur:
            with cur.copy("copy embeddings (id, entity_id, chunk, vec) from stdin") as cp:
                for i, v in enumerate(X):
                    cp.write_row((ids[i], ids[i], i, vec_literal(v)))
        r["insert_20k_with_index_s"] = round(time.time() - t0, 2)
        r["rows_node1"] = c.execute("select count(*) from embeddings").fetchone()[0]
    Q = np.random.default_rng(7).standard_normal((100, 384)).astype(np.float32)
    Q /= np.linalg.norm(Q, axis=1, keepdims=True)
    # exact top-10 by cosine distance (== max dot product for unit vectors)
    sims = Q @ X.T
    truth = [set(np.argsort(-s)[:10].tolist()) for s in sims]
    idx_of = {str(u): i for i, u in enumerate(ids)}

    def run_queries(ef):
        lat, recalls = [], []
        with connect("node2", stmt_timeout_ms=60000) as c:
            if ef is not None:
                c.execute(f"set hnsw.ef_search = {ef}")
            plan = "\n".join(r_[0] for r_ in c.execute(
                "explain select id from embeddings order by vec <=> %s::vector limit 10", (vec_literal(Q[0]),)))
            for q, tr in zip(Q, truth):
                lit = vec_literal(q)
                t = time.time()
                rows = c.execute("select id from embeddings order by vec <=> %s::vector limit 10", (lit,)).fetchall()
                lat.append(time.time() - t)
                got = {idx_of[str(x[0])] for x in rows}
                recalls.append(len(got & tr) / 10)
        return {"ef_search": ef if ef is not None else "default(40)", "p50_ms": ms(pct(lat, 50)),
                "p95_ms": ms(pct(lat, 95)), "recall_at_10": round(float(np.mean(recalls)), 4),
                "min_recall": min(recalls), "plan": plan}

    with connect("node2") as c:
        r["rows_node2"] = c.execute("select count(*) from embeddings").fetchone()[0]
        r["ef_search_default"] = c.execute("show hnsw.ef_search").fetchone()[0]
    r["queries_node2"] = [run_queries(None), run_queries(100)]
    # exact (no index) latency for reference
    lat = []
    with connect("node2", stmt_timeout_ms=120000) as c:
        c.execute("set enable_indexscan = off")
        for q in Q[:10]:
            t = time.time()
            c.execute("select id from embeddings order by vec <=> %s::vector limit 10", (vec_literal(q),)).fetchall()
            lat.append(time.time() - t)
    r["exact_seqscan_node2_p50_ms_10_queries"] = ms(pct(lat, 50))
    # index build time on the populated table
    with connect("node1", stmt_timeout_ms=0) as c:
        t0 = time.time()
        try:
            c.execute("create index nonconcurrently embeddings_vec_build_test on embeddings "
                      "using ybhnsw (vec vector_cosine_ops) with (m = 16, ef_construction = 64)")
            r["index_build_on_20k_rows_s"] = round(time.time() - t0, 2)
            c.execute("drop index embeddings_vec_build_test")
        except Exception as e:  # noqa: BLE001
            r["index_build_on_20k_rows_error"] = err_str(e)
    r["index_build_on_empty_table_in_0002_s"] = RESULTS.get("S5", {}).get(
        "upgrade_head_on_node1_with_node3_stopped", {}).get("seconds")
    RESULTS["S4"] = r
    log("S4", json.dumps({k: v for k, v in r.items()}, default=str)[:2500])


# ---------------------------------------------------------------- S6
def s6_footprint():
    log("S6 footprint (idle 60s first)")
    time.sleep(60)
    r = {"docker_stats": {}, "disk": {}, "processes": {}}
    for sample in range(3):
        out = sh("docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
                 *[container(n) for n in ("node1", "node2", "node3")]).stdout
        for line in out.strip().splitlines():
            name, cpu, mem = line.split("\t")
            r["docker_stats"].setdefault(name, []).append({"cpu": cpu, "mem": mem})
        time.sleep(5)
    for n in ("node1", "node2", "node3"):
        r["disk"][n] = sh("docker", "exec", container(n), "du", "-sh", "/home/yugabyte/ybd/data",
                          check=False).stdout.strip()
        r["processes"][n] = sh("docker", "exec", container(n), "bash", "-c",
                               "ps -eo rss,comm --sort=-rss | awk 'NR>1{a[$2]+=$1; c[$2]++} END "
                               "{for (k in a) printf \"%s x%d %.0fMiB\\n\", k, c[k], a[k]/1024}' | sort",
                               check=False).stdout.strip().splitlines()
    r["image"] = IMAGE
    r["image_size_bytes"] = int(sh("docker", "image", "inspect", IMAGE, "--format", "{{.Size}}").stdout.strip())
    r["configured_limits"] = {"master memory_limit_hard_bytes": 402653184, "tserver memory_limit_hard_bytes": 805306368,
                              "tserver db_block_cache_size_bytes": 67108864,
                              "master db_block_cache_size_bytes": 33554432, "docker mem_limit": "2g"}
    RESULTS["S6"] = r
    log("S6", json.dumps(r)[:2500])


# ---------------------------------------------------------------- S7
def s7_backup():
    log("S7 backup/restore via ysql_dump")
    r = {"method": "ysql_dump (pg_dump fork) logical dump of database yugabyte from node1, "
                   "restored with ysqlsh into a fresh single-node yugabyted container"}
    counts_src = {}
    with connect("node1") as c:
        for t in ("tasks", "jobs", "embeddings", "alembic_version"):
            counts_src[t] = c.execute(f"select count(*) from {t}").fetchone()[0]
    t0 = time.time()
    dump = OUT / "node1.sql"
    with open(dump, "w") as f:
        p = subprocess.run(["docker", "exec", container("node1"), "bash", "-c",
                            "postgres/bin/ysql_dump -h node1-cluster -U yugabyte -d yugabyte"],
                           stdout=f, stderr=subprocess.PIPE, text=True)
    r["dump_rc"] = p.returncode
    r["dump_stderr"] = p.stderr[-500:]
    r["dump_seconds"] = round(time.time() - t0, 1)
    r["dump_bytes"] = dump.stat().st_size
    compose("--profile", "restore", "up", "-d", "restore", timeout=600)
    wait_ysql("restore", timeout=300)
    t0 = time.time()
    with open(dump) as f:
        p = subprocess.run(["docker", "exec", "-i", container("restore"), "bash", "-c",
                            "bin/ysqlsh -h $(hostname -i | awk '{print $1}') -U yugabyte -d yugabyte "
                            "-v ON_ERROR_STOP=0 -q"],
                           stdin=f, capture_output=True, text=True)
    r["restore_seconds"] = round(time.time() - t0, 1)
    r["restore_rc"] = p.returncode
    r["restore_errors"] = [l for l in p.stderr.splitlines() if "ERROR" in l][:10]
    counts_dst = {}
    with connect("restore") as c:
        for t in ("tasks", "jobs", "embeddings", "alembic_version"):
            try:
                counts_dst[t] = c.execute(f"select count(*) from {t}").fetchone()[0]
            except Exception as e:  # noqa: BLE001
                counts_dst[t] = err_str(e)
        r["restored_indexes"] = [list(x) for x in c.execute(
            "select indexname, indexdef from pg_indexes where schemaname='public' order by 1")]
        r["restored_alembic_version"] = [x[0] for x in c.execute("select version_num from alembic_version")]
    r["counts_source"] = counts_src
    r["counts_restored"] = counts_dst
    r["counts_match"] = counts_src == counts_dst
    # distributed snapshot (in-cluster, the basis of YB's physical backups)
    snap = yb_admin("create_database_snapshot", "ysql.yugabyte")
    r["yb_admin_create_database_snapshot"] = (snap.stdout + snap.stderr).strip()[-300:]
    time.sleep(5)
    r["yb_admin_list_snapshots"] = yb_admin("list_snapshots").stdout.strip()[-600:]
    compose("--profile", "restore", "rm", "-s", "-f", "-v", "restore", check=False)
    RESULTS["S7"] = r
    log("S7", json.dumps(r, default=str)[:2500])


# ---------------------------------------------------------------- main
def save():
    RESULTS["run_date"] = time.strftime("%Y-%m-%d")
    (HERE / "results.json").write_text(json.dumps(RESULTS, indent=1, default=str))


def run_all(args):
    OUT.mkdir(exist_ok=True)
    steps = [("setup", setup_cluster), ("S5", s5_alembic), ("S5x", s5_extra_transactional_ddl),
             ("S1", s1_conflicts), ("S2", s2_offline), ("S3", s3_jobs), ("S4", s4_vectors),
             ("S6", s6_footprint), ("S7", s7_backup), ("S2b", s2b_two_down)]
    only = set(args.only.split(",")) if args.only else None
    for name, fn in steps:
        if only and name not in only:
            continue
        t0 = time.time()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log(f"{name} FAILED", traceback.format_exc())
            RESULTS.setdefault("failures", {})[name] = traceback.format_exc()[-2000:]
        RESULTS.setdefault("step_seconds", {})[name] = round(time.time() - t0, 1)
        save()
    save()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("all")
    a.add_argument("--only", default="")
    w = sub.add_parser("worker")
    w.add_argument("--name", required=True)
    w.add_argument("--node", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--max-seconds", type=float, default=420)
    args = ap.parse_args()
    if args.cmd == "all":
        run_all(args)
    else:
        worker_main(args)


if __name__ == "__main__":
    main()
