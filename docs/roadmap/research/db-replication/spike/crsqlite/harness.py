"""cr-sqlite spike harness: runs S5, S1, S2, S3, S4, S6, S7 against the
three-node compose cluster (S5 first, because it creates the schema the other
scenarios need). Writes results.json next to this file."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

HERE = Path(__file__).resolve().parent
PROJECT = "spike-crsqlite"
PORTS = {"node1": 16101, "node2": 16102, "node3": 16103}
IMAGE = "spike-crsqlite-node:latest"
RESULTS = {}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def iso(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sh(*args, check=True, env=None):
    p = subprocess.run(args, cwd=HERE, capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args}: {p.stderr}")
    return p.stdout


def compose(*args, **kw):
    return sh("docker", "compose", "-p", PROJECT, *args, **kw)


def container(node):
    return f"{PROJECT}-{node}-1"


class Node:
    def __init__(self, name, port=None):
        self.name = name
        self.c = httpx.Client(base_url=f"http://127.0.0.1:{port or PORTS[name]}", timeout=120)

    def sql(self, sql, params=(), many=None):
        body = {"sql": sql, "params": list(params)}
        if many is not None:
            body["many"] = many
        r = self.c.post("/sql", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"{self.name}: {r.text}")
        return r.json()

    def rows(self, sql, params=()):
        return self.sql(sql, params)["rows"]

    def one(self, sql, params=()):
        return self.rows(sql, params)[0][0]

    def status(self):
        return self.c.get("/status").json()

    def migrate(self, target):
        t = time.time()
        r = self.c.post("/migrate", json={"target": target}, timeout=600).json()
        r["seconds"] = round(time.time() - t, 3)
        return r

    def wait_up(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.status()
                return True
            except Exception:
                time.sleep(0.2)
        raise TimeoutError(self.name)


N = {k: Node(k) for k in PORTS}


def b64(b):
    return {"$b": base64.b64encode(b).decode()}


def wait_for(pred, timeout, step=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if pred():
                return time.time() - t0
        except Exception:
            pass
        time.sleep(step)
    return None


def tables(node):
    return sorted(r[0] for r in node.rows(
        "SELECT name FROM sqlite_master WHERE type IN ('table') AND name NOT LIKE '%crsql%' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_%' ESCAPE '\\' "
        "AND name NOT LIKE 'vec_embeddings_%'"))


def alembic_version(node):
    try:
        return node.one("SELECT version_num FROM alembic_version")
    except Exception as e:
        return f"none ({str(e)[:80]})"


def columns(node, table):
    return [r[1] for r in node.rows(f"PRAGMA table_info({table})")]


# ---------------------------------------------------------------- S5
def s5():
    log("S5: Alembic across the cluster")
    r = {"steps": []}
    m = N["node1"].migrate("0001_initial")
    r["steps"].append({"step": "alembic upgrade 0001_initial on node1 (via node /migrate)", "rc": m["rc"], "seconds": m["seconds"]})
    time.sleep(5)
    r["after_0001_on_node1_only"] = {
        n: {"tables": tables(N[n]), "alembic_version": alembic_version(N[n])} for n in N}
    # Replication of rows needs the schema on every node: run Alembic per node.
    for n in ("node2", "node3"):
        m = N[n].migrate("0001_initial")
        r["steps"].append({"step": f"alembic upgrade 0001_initial on {n}", "rc": m["rc"], "seconds": m["seconds"]})
    tid = str(uuid.uuid4())
    N["node1"].sql("INSERT INTO tasks(id, owner, title, status, updated_at) VALUES (?,?,?,?,?)",
                   [tid, "alice", "s5-probe", "open", iso()])
    r["probe_row_replicated_s"] = wait_for(
        lambda: all(N[n].one("SELECT count(*) FROM tasks WHERE id=?", [tid]) == 1 for n in N), 30)

    compose("stop", "node3")
    r["steps"].append({"step": "docker compose stop node3"})
    m = N["node1"].migrate("head")
    r["steps"].append({"step": "alembic upgrade head on node1", "rc": m["rc"], "seconds": m["seconds"], "stderr": m["err"][-600:]})
    r["node1_after_head"] = {"alembic_version": alembic_version(N["node1"]),
                             "tasks_columns": columns(N["node1"], "tasks"),
                             "tables": tables(N["node1"])}
    # Mixed versions: node2 still at 0001 receives a change for tasks.priority.
    pid = str(uuid.uuid4())
    N["node1"].sql("INSERT INTO tasks(id, owner, title, status, updated_at, priority) VALUES (?,?,?,?,?,?)",
                   [pid, "alice", "s5-priority", "open", iso(), 5])
    time.sleep(5)
    st2 = N["node2"].status()
    r["mixed_versions_node2_at_0001"] = {
        "row_arrived_on_node2": N["node2"].one("SELECT count(*) FROM tasks WHERE id=?", [pid]) == 1,
        "node2_sync_error_from_node1": st2["errors"].get("node1"),
    }
    m = N["node2"].migrate("head")
    r["steps"].append({"step": "alembic upgrade head on node2", "rc": m["rc"], "seconds": m["seconds"]})
    r["node2_after_head_priority_row_s"] = wait_for(
        lambda: N["node2"].one("SELECT priority FROM tasks WHERE id=?", [pid]) == 5, 30)

    t = time.time()
    compose("start", "node3")
    N["node3"].wait_up()
    r["steps"].append({"step": "docker compose start node3 (entrypoint runs `alembic upgrade head` because the node already has alembic_version)"})
    r["node3_priority_row_s"] = wait_for(
        lambda: N["node3"].one("SELECT priority FROM tasks WHERE id=?", [pid]) == 5, 60)
    r["node3_start_to_priority_row_s"] = round(time.time() - t, 3)
    r["node3_after_start"] = {
        "alembic_version": alembic_version(N["node3"]),
        "tasks_columns": columns(N["node3"], "tasks"),
        "has_vec_index": N["node3"].one("SELECT count(*) FROM sqlite_master WHERE name='vec_embeddings'") == 1,
        "vec_index_sql": N["node3"].one("SELECT sql FROM sqlite_master WHERE name='vec_embeddings'"),
    }
    r["node3_log_tail"] = sh("docker", "logs", "--tail", "8", container("node3"), check=False)[-1500:] + \
        subprocess.run(["docker", "logs", "--tail", "8", container("node3")], capture_output=True, text=True).stderr[-1500:]
    r["schema_replicates"] = r["after_0001_on_node1_only"]["node2"]["tables"] != []
    try:
        r["batch_recreate_probe"] = json.loads(sh("docker", "exec", container("node1"), "python", "/app/probe_batch_alter.py"))
    except Exception as e:
        r["batch_recreate_probe"] = f"error: {e}"
    RESULTS["S5"] = r
    log(json.dumps(r, indent=1)[:3000])


# ---------------------------------------------------------------- S1
def s1_trial(label, v1, v2, partitioned=False):
    tid = str(uuid.uuid4())
    N["node1"].sql("INSERT INTO tasks(id, owner, title, status, updated_at) VALUES (?,?,?,?,?)",
                   [tid, "alice", "original", "open", iso()])
    rep = wait_for(lambda: all(N[n].one("SELECT count(*) FROM tasks WHERE id=?", [tid]) for n in N), 30)
    res = {}
    clients = {n: Node(n) for n in ("node1", "node2")}

    def w(n, val, bar=None):
        if bar:
            bar.wait()
        t = time.time()
        try:
            r = clients[n].sql("UPDATE tasks SET title=?, version=version+1, updated_at=? WHERE id=?", [val, iso(), tid])
            res[n] = {"ok": r["rowcount"] == 1, "error": None, "t": t, "ms": round((time.time() - t) * 1000, 2)}
        except Exception as e:
            res[n] = {"ok": False, "error": str(e), "t": t}

    if partitioned:
        # node2 cut off from the cluster; node1 edits the title twice more, then
        # node2 writes once, 1 s later in wall-clock time; then heal.
        sh("docker", "network", "disconnect", f"{PROJECT}_cluster", container("node2"))
        for i in range(2):
            N["node1"].sql("UPDATE tasks SET title=?, version=version+1 WHERE id=?", [f"node1 draft {i}", tid])
        w("node1", v1)
        time.sleep(1)
        w("node2", v2)
        sh("docker", "network", "connect", "--alias", "node2-cluster", f"{PROJECT}_cluster", container("node2"))
    else:
        bar = threading.Barrier(2)
        ts = [threading.Thread(target=w, args=(n, v, bar)) for n, v in (("node1", v1), ("node2", v2))]
        [t.start() for t in ts]
        [t.join() for t in ts]
    t0 = max(x["t"] for x in res.values())

    def same():
        vals = [N[n].rows("SELECT title, version FROM tasks WHERE id=?", [tid])[0] for n in N]
        return all(v == vals[0] for v in vals)

    conv = wait_for(same, 60, step=0.01)
    conv_s = None if conv is None else round(time.time() - t0, 3)
    final = {n: N[n].rows("SELECT title, version FROM tasks WHERE id=?", [tid])[0] for n in N}
    try:
        clock = N["node1"].rows(
            "SELECT cid, col_version, hex(site_id) FROM crsql_changes WHERE \"table\"='tasks' "
            "AND pk=crsql_pack_columns(?) AND cid IN ('title','version')", [tid])
    except Exception as e:
        clock = str(e)
    return {"label": label, "node1_wrote": v1, "node2_wrote": v2, "replicated_before_s": rep,
            "writes": {n: {k: v for k, v in x.items() if k != "t"} for n, x in res.items()},
            "final": final, "converged": conv is not None, "convergence_s": conv_s,
            "node1_clock_after": clock}


def s1():
    log("S1: conflicting writes")
    r = {"trials": [
        s1_trial("simultaneous, node2 value sorts higher", "A title from node1", "B title from node2"),
        s1_trial("simultaneous, node1 value sorts higher", "B title from node1", "A title from node2"),
        s1_trial("node2 partitioned; node1 edits title 3x, node2 edits once 1 s later; heal",
                 "A node1 third edit", "B node2 later edit", partitioned=True),
    ]}
    r["site_ids"] = {n: N[n].status()["site_id"] for n in N}
    RESULTS["S1"] = r
    log(json.dumps(r, indent=1)[:4000])


# ---------------------------------------------------------------- S2
def checksum(node):
    rows = node.rows("SELECT id, title FROM tasks ORDER BY id")
    h = hashlib.md5()
    for i, t in rows:
        h.update(f"{i}|{t}\n".encode())
    return len(rows), h.hexdigest()


def s2():
    log("S2: laptop offline and catch-up")
    r = {}
    compose("stop", "node3")
    lat, errors, per = [], [], {"node1": 0, "node2": 0}
    t0 = time.time()
    for i in range(10000):
        n = "node1" if i % 2 == 0 else "node2"
        t = time.perf_counter()
        try:
            N[n].sql("INSERT INTO tasks(id, owner, title, status, updated_at) VALUES (?,?,?,?,?)",
                     [str(uuid.uuid4()), "alice", f"s2 task {i}", "open", iso()])
            per[n] += 1
        except Exception as e:
            errors.append(str(e)[:200])
        lat.append((time.perf_counter() - t) * 1000)
    r["insert_wall_s"] = round(time.time() - t0, 2)
    r["write_errors"] = len(errors)
    r["error_samples"] = errors[:5]
    r["writes_ok"] = per
    r["p50_ms"] = round(float(np.percentile(lat, 50)), 2)
    r["p95_ms"] = round(float(np.percentile(lat, 95)), 2)
    r["p99_ms"] = round(float(np.percentile(lat, 99)), 2)
    c1 = wait_for(lambda: checksum(N["node1"]) == checksum(N["node2"]), 120, step=0.5)
    r["node1_node2_converged_after_inserts_s"] = c1
    target = checksum(N["node1"])
    r["expected_rows"] = target[0]
    t = time.time()
    compose("start", "node3")
    N["node3"].wait_up()
    r["node3_process_up_s"] = round(time.time() - t, 3)
    count_s = wait_for(lambda: N["node3"].one("SELECT count(*) FROM tasks") == target[0], 600, step=0.1)
    r["node3_all_rows_s"] = None if count_s is None else round(time.time() - t, 3)
    ok = wait_for(lambda: checksum(N["node3"]) == target, 120, step=0.2)
    r["node3_identical_checksum_s"] = None if ok is None else round(time.time() - t, 3)
    r["checksums"] = {n: checksum(N[n]) for n in N}
    st = N["node3"].status()
    r["node3_changes_applied"] = st["applied"]
    RESULTS["S2"] = r
    log(json.dumps(r, indent=1))


# ---------------------------------------------------------------- S3
def run_jobs(mode, label, partition):
    kind = f"s3-{mode}-{label}"
    ids = [str(uuid.uuid4()) for _ in range(200)]
    t = iso()
    body = {"sql": "INSERT INTO jobs(id, kind, run_at) VALUES (?,?,?)", "many": [[i, kind, t] for i in ids]}
    N["node1"].c.post("/sql", json=body).raise_for_status()
    wait_for(lambda: all(N[n].one("SELECT count(*) FROM jobs WHERE kind=?", [kind]) == 200 for n in N), 60)
    work = Path(tempfile.mkdtemp(prefix="crsqlite-s3-"))
    stop = work / "stop"
    if not partition:
        stop.touch()
    procs = []
    for w, n in (("w1", "node1"), ("w2", "node2")):
        procs.append(subprocess.Popen([
            sys.executable, str(HERE / "worker.py"), "--url", f"http://127.0.0.1:{PORTS[n]}", "--node", n,
            "--name", w, "--mode", mode, "--kind", kind, "--out", str(work / f"fired-{w}.log"),
            "--stopfile", str(stop), "--stats", str(work / f"stats-{w}.json")]))
    t0 = time.time()
    r = {"mode": mode, "partition": partition}

    def fired_lines():
        return sum(len(p.read_text().splitlines()) for p in work.glob("fired-*.log"))

    if partition:
        wait_for(lambda: fired_lines() >= 100, 300, step=0.02)
        r["fired_before_partition"] = fired_lines()
        sh("docker", "network", "disconnect", f"{PROJECT}_cluster", container("node1"))
        tp = time.time()
        r["partitioned_at_s"] = round(tp - t0, 2)
        time.sleep(60)
        r["fired_during_partition"] = fired_lines() - r["fired_before_partition"]
        sh("docker", "network", "connect", "--alias", "node1-cluster", f"{PROJECT}_cluster", container("node1"))
        r["healed_at_s"] = round(time.time() - t0, 2)
        stop.touch()
    for p in procs:
        try:
            p.wait(timeout=400)
        except subprocess.TimeoutExpired:
            p.kill()
    r["workers_finished_s"] = round(time.time() - t0, 2)
    fires = {}
    for f in work.glob("fired-*.log"):
        for line in f.read_text().splitlines():
            jid, w = line.split(",")
            fires.setdefault(jid, []).append(w)
    r["exactly_once"] = sum(1 for i in ids if len(fires.get(i, [])) == 1)
    r["more_than_once"] = sum(1 for i in ids if len(fires.get(i, [])) > 1)
    r["never"] = sum(1 for i in ids if len(fires.get(i, [])) == 0)
    r["duplicate_fires_total"] = sum(len(v) - 1 for v in fires.values() if len(v) > 1)
    r["per_worker"] = {w: json.loads((work / f"stats-{w}.json").read_text()) for w in ("w1", "w2")
                       if (work / f"stats-{w}.json").exists()}
    conv = wait_for(lambda: len({json.dumps(N[n].rows(
        "SELECT id, lease_owner, done_at IS NOT NULL FROM jobs WHERE kind=? ORDER BY id", [kind])) for n in N}) == 1, 60, step=0.5)
    r["jobs_table_converged_after_s"] = conv
    r["jobs_done_on_node1"] = N["node1"].one("SELECT count(*) FROM jobs WHERE kind=? AND done_at IS NOT NULL", [kind])
    shutil.rmtree(work, ignore_errors=True)
    log(json.dumps(r, indent=1))
    return r


def s3():
    log("S3: exactly-once jobs")
    RESULTS["S3"] = {
        "barrier_a_healthy": run_jobs("barrier", "a", False),
        "barrier_b_partition": run_jobs("barrier", "b", True),
        "vote_a_healthy": run_jobs("vote", "a", False),
        "vote_b_partition": run_jobs("vote", "b", True),
    }


# ---------------------------------------------------------------- S4
def s4():
    log("S4: vector search")
    r = {}
    D, NV = 384, 20000
    X = np.random.default_rng(42).standard_normal((NV, D)).astype(np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    Q = np.random.default_rng(7).standard_normal((100, D)).astype(np.float32)
    Q /= np.linalg.norm(Q, axis=1, keepdims=True)
    ids = [str(uuid.UUID(int=i + 1)) for i in range(NV)]
    r["index_sql"] = N["node1"].one("SELECT sql FROM sqlite_master WHERE name='vec_embeddings'")
    t = time.time()
    for s in range(0, NV, 500):
        body = {"sql": "INSERT INTO embeddings(id, entity_id, chunk, vec) VALUES (?,?,?,?)",
                "many": [[ids[i], str(uuid.uuid4()), 0, b64(X[i].tobytes())] for i in range(s, min(NV, s + 500))]}
        N["node1"].c.post("/sql", json=body).raise_for_status()
    r["node1_insert_and_index_build_s"] = round(time.time() - t, 2)
    r["node1_vec_index_rows"] = N["node1"].one("SELECT count(*) FROM vec_embeddings")
    tr = wait_for(lambda: N["node2"].one("SELECT count(*) FROM vec_embeddings") == NV, 1800, step=1)
    r["node2_index_complete_after_node1_done_s"] = None if tr is None else round(tr, 2)
    r["node2_embeddings_rows"] = N["node2"].one("SELECT count(*) FROM embeddings")
    r["node2_vec_index_rows"] = N["node2"].one("SELECT count(*) FROM vec_embeddings")
    gt = np.argsort(-(Q @ X.T), axis=1)[:, :10]
    ann = ("SELECT e.id FROM (SELECT rowid, distance FROM vec_embeddings WHERE vec MATCH ? AND k = 10) v "
           "JOIN embeddings e ON e.rowid = v.rowid ORDER BY v.distance")
    exact = "SELECT id FROM embeddings ORDER BY vec_distance_cosine(vec, ?) LIMIT 10"

    def bench(sql):
        lat, rec = [], []
        for qi in range(100):
            t = time.perf_counter()
            got = [x[0] for x in N["node2"].rows(sql, [b64(Q[qi].tobytes())])]
            lat.append((time.perf_counter() - t) * 1000)
            want = {ids[j] for j in gt[qi]}
            rec.append(len(want & set(got)) / 10)
        return {"p50_ms": round(float(np.percentile(lat, 50)), 2), "p95_ms": round(float(np.percentile(lat, 95)), 2),
                "recall_at_10": round(float(np.mean(rec)), 4)}

    r["node2_diskann_default"] = bench(ann)
    N["node2"].sql("INSERT INTO vec_embeddings(vec_embeddings) VALUES ('search_list_size_search=512')")
    r["node2_diskann_search_list_512"] = bench(ann)
    r["node2_exact_scan_of_crr_table"] = bench(exact)
    r["usable_on_node2_without_extra_steps"] = r["node2_vec_index_rows"] == NV
    RESULTS["S4"] = r
    log(json.dumps(r, indent=1))


# ---------------------------------------------------------------- S6
def s6():
    log("S6: footprint")
    time.sleep(20)
    r = {"nodes": {}}
    samples = []
    for _ in range(3):
        out = sh("docker", "stats", "--no-stream", "--format", "{{json .}}", *[container(n) for n in N])
        samples.append([json.loads(line) for line in out.splitlines()])
        time.sleep(2)
    for n in N:
        c = container(n)
        rows = [s for smp in samples for s in smp if s["Name"] == c]
        du = sh("docker", "exec", c, "sh", "-c", "du -sb /data; ls -l /data").strip()
        rss = sh("docker", "exec", c, "sh", "-c", "grep -E 'VmRSS|VmHWM' /proc/1/status").split()
        r["nodes"][n] = {"mem": [x["MemUsage"] for x in rows], "cpu": [x["CPUPerc"] for x in rows], "data_du": du,
                         "python_process": " ".join(rss)}
    r["image_size_bytes"] = int(sh("docker", "image", "inspect", IMAGE, "--format", "{{.Size}}").strip())
    r["image_ls"] = sh("docker", "image", "ls", IMAGE, "--format", "{{.Repository}}:{{.Tag}} {{.Size}}").strip()
    RESULTS["S6"] = r
    log(json.dumps(r, indent=1))


# ---------------------------------------------------------------- S7
COUNT_TABLES = ["tasks", "jobs", "embeddings", "job_votes", "vec_embeddings"]


def s7():
    log("S7: backup and restore")
    r = {"method": "VACUUM INTO on the live node (online, consistent snapshot) -> copy file"}
    src = {t: N["node1"].one(f"SELECT count(*) FROM {t}") for t in COUNT_TABLES}
    b = N["node1"].c.post("/backup", json={"path": "/tmp/backup.db"}, timeout=600).json()
    r["backup"] = b
    d = Path(tempfile.mkdtemp(prefix="crsqlite-restore-"))
    sh("docker", "cp", f"{container('node1')}:/tmp/backup.db", str(d / "titan.db"))
    os.chmod(d, 0o777)
    env = dict(os.environ, RESTORE_DIR=str(d))
    t = time.time()
    compose("--profile", "restore", "up", "-d", "restore", env=env)
    R = Node("restore", 16109)
    R.wait_up()
    r["restore_start_s"] = round(time.time() - t, 2)
    dst = {t: R.one(f"SELECT count(*) FROM {t}") for t in COUNT_TABLES}
    r["counts_node1_at_backup"] = src
    r["counts_restored"] = dst
    r["counts_match"] = src == dst
    r["restored_alembic_version"] = alembic_version(R)
    r["restored_site_id_equals_node1"] = R.status()["site_id"] == N["node1"].status()["site_id"]
    q = np.random.default_rng(7).standard_normal(384).astype(np.float32)
    try:
        r["restored_ann_query_rows"] = len(R.rows("SELECT rowid FROM vec_embeddings WHERE vec MATCH ? AND k=10", [b64((q / np.linalg.norm(q)).tobytes())]))
    except Exception as e:
        r["restored_ann_query_rows"] = f"error: {e}"
    compose("--profile", "restore", "rm", "-sf", "restore", env=env)
    shutil.rmtree(d, ignore_errors=True)
    RESULTS["S7"] = r
    log(json.dumps(r, indent=1))


def main():
    only = sys.argv[1:] or ["s5", "s1", "s2", "s3", "s4", "s6", "s7"]
    for n in N.values():
        n.wait_up()
    RESULTS["run_started"] = iso()
    try:
        for s in only:
            t = time.time()
            globals()[s]()
            RESULTS.setdefault("scenario_wall_s", {})[s.upper()] = round(time.time() - t, 1)
    finally:
        RESULTS["run_finished"] = iso()
        RESULTS["versions"] = {
            "node_image": IMAGE,
            "sqlite": N["node1"].one("SELECT sqlite_version()"),
            "sqlite_vec": N["node1"].one("SELECT vec_version()"),
            "crsqlite": "v0.16.3 (crsqlite-linux-x86_64.zip from GitHub releases)",
        }
        (HERE / "results.json").write_text(json.dumps(RESULTS, indent=1, default=str))
        log("wrote results.json")


if __name__ == "__main__":
    main()
