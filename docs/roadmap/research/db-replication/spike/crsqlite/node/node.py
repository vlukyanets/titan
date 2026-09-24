"""cr-sqlite spike node.

One process per node. It owns a local SQLite file with the cr-sqlite and
sqlite-vec extensions loaded and exposes a small HTTP API:

  POST /sql       run SQL for the harness (client network)
  GET  /changes   changeset feed for peers (long-poll; cluster network)
  POST /barrier   wait until reachable peers pulled db_version >= v, then pull
                  once from every reachable peer (used by the S3 lease)
  POST /migrate   pause sync, close the connection, run `alembic upgrade`, reopen
  POST /backup    VACUUM INTO a file
  GET  /status    site id, db_version, watermarks, sync errors

Replication: one thread per peer loops on GET <peer>/changes?since=<wm>, applies
the rows with INSERT INTO crsql_changes in one transaction and stores the new
per-peer watermark (the peer's local db_version) in the same transaction.
"""

import base64
import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sqlite_vec

NODE = os.environ.get("NODE_NAME", "node")
DB_PATH = os.environ.get("TITAN_DB_PATH", "/data/titan.db")
CRSQLITE = os.environ.get("CRSQLITE_PATH", "/opt/ext/crsqlite")
PEERS = dict(p.split("=", 1) for p in os.environ.get("PEERS", "").split(",") if p)
PAGE = int(os.environ.get("SYNC_PAGE", "5000"))
LONGPOLL = float(os.environ.get("SYNC_LONGPOLL", "2"))
REACHABLE_WINDOW = float(os.environ.get("REACHABLE_WINDOW", "4"))
CHANGE_COLS = '"table","pk","cid","val","col_version","db_version","site_id","cl","seq"'

lock = threading.RLock()
cond = threading.Condition(lock)
conn = None
paused = False
site_hex = ""
state = {
    "last_seen": {p: 0.0 for p in PEERS},  # last successful contact either direction
    "acked": {p: 0 for p in PEERS},  # highest `since` a peer asked us for
    "errors": {p: None for p in PEERS},
    "applied": {p: 0 for p in PEERS},
    "vote_hook_errors": 0,
}


# ---------------------------------------------------------------- database
def open_db():
    global conn, site_hex
    c = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=30)
    c.enable_load_extension(True)
    c.load_extension(CRSQLITE)
    sqlite_vec.load(c)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute(
        "CREATE TABLE IF NOT EXISTS _sync_peer(peer TEXT PRIMARY KEY, wm INTEGER NOT NULL)"
    )
    site_hex = c.execute("SELECT hex(crsql_site_id())").fetchone()[0]
    conn = c


def close_db():
    global conn
    if conn is not None:
        conn.execute("SELECT crsql_finalize()")
        conn.close()
        conn = None


def enc(v):
    if isinstance(v, (bytes, memoryview)):
        return {"$b": base64.b64encode(bytes(v)).decode()}
    return v


def dec(v):
    if isinstance(v, dict) and "$b" in v:
        return base64.b64decode(v["$b"])
    return v


def db_version():
    return conn.execute("SELECT crsql_db_version()").fetchone()[0]


def run_sql(req):
    """Run one statement, executemany, or a list of statements in one tx."""
    with lock:
        if paused:
            raise RuntimeError("node paused for migration")
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            rows, cols, rc = [], None, 0
            stmts = req.get("tx") or [[req["sql"], req.get("params") or []]]
            for sql, params in stmts:
                if req.get("many") is not None:
                    cur.executemany(sql, [[dec(x) for x in p] for p in req["many"]])
                else:
                    cur.execute(sql, [dec(x) for x in params])
                rc = cur.rowcount
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    rows = [[enc(x) for x in r] for r in cur.fetchall()]
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        v = db_version()
        cond.notify_all()
    return {"rows": rows, "columns": cols, "rowcount": rc, "db_version": v}


def changes_since(since, exclude_site_hex):
    exclude = bytes.fromhex(exclude_site_hex)
    """One page of changes with db_version > since, cut at a db_version boundary."""
    q = (
        f"SELECT {CHANGE_COLS} FROM crsql_changes WHERE db_version > ? "
        "AND site_id IS NOT ? ORDER BY db_version, seq LIMIT ?"
    )
    rows = conn.execute(q, (since, exclude, PAGE)).fetchall()
    # Callers hold `lock` (single connection), so nothing commits in between.
    top = conn.execute("SELECT crsql_db_version()").fetchone()[0]
    if len(rows) == PAGE:
        last = rows[-1][5]
        if rows[0][5] == last:  # a single huge transaction: send all of it
            rows = conn.execute(
                f"SELECT {CHANGE_COLS} FROM crsql_changes WHERE db_version = ? "
                "AND site_id IS NOT ? ORDER BY seq",
                (last, exclude),
            ).fetchall()
            return rows, last, True
        rows = [r for r in rows if r[5] != last]
        return rows, rows[-1][5], True
    return rows, (top or since), False


# Post-merge hook for the quorum-vote lease (S3 variant B): this node casts its
# single vote for every job it has seen a claim for and has not voted on yet.
VOTE_HOOK = """
INSERT OR IGNORE INTO job_votes(job_id, voter, candidate, voted_at)
SELECT v.job_id, :me, min(v.candidate), strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM job_votes v
WHERE NOT EXISTS (SELECT 1 FROM job_votes m WHERE m.job_id = v.job_id AND m.voter = :me)
GROUP BY v.job_id
"""


def apply_changes(peer, payload):
    rows = [[dec(x) for x in r] for r in payload["changes"]]
    new_wm = payload["wm"]
    with lock:
        if paused:
            return 0
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            old = cur.execute("SELECT wm FROM _sync_peer WHERE peer=?", (peer,)).fetchone()
            old = old[0] if old else 0
            if rows:
                cur.executemany(
                    f"INSERT INTO crsql_changes ({CHANGE_COLS}) VALUES (?,?,?,?,?,?,?,?,?)", rows
                )
            cur.execute(
                "INSERT INTO _sync_peer(peer, wm) VALUES(?, ?) "
                "ON CONFLICT(peer) DO UPDATE SET wm = max(wm, excluded.wm)",
                (peer, max(old, new_wm)),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        if rows and any(r[0] == "job_votes" for r in rows):
            try:
                conn.execute(VOTE_HOOK, {"me": NODE})
            except sqlite3.Error:
                state["vote_hook_errors"] += 1
        cond.notify_all()
    state["applied"][peer] += len(rows)
    return len(rows)


def get_wm(peer):
    with lock:
        r = conn.execute("SELECT wm FROM _sync_peer WHERE peer=?", (peer,)).fetchone()
    return r[0] if r else 0


def fetch(peer, wait):
    q = urllib.parse.urlencode(
        {"since": get_wm(peer), "site": site_hex, "peer": NODE, "wait": wait}
    )
    with urllib.request.urlopen(f"{PEERS[peer]}/changes?{q}", timeout=wait + 10) as r:
        payload = json.loads(r.read())
    state["last_seen"][peer] = time.time()
    return payload


def pull_once(peer, wait=0.0):
    while True:
        payload = fetch(peer, wait)
        apply_changes(peer, payload)
        state["errors"][peer] = None
        if not payload["more"]:
            return
        wait = 0.0


def peer_loop(peer):
    while True:
        if paused or conn is None:
            time.sleep(0.2)
            continue
        try:
            pull_once(peer, LONGPOLL)
        except Exception as e:  # network down, schema mismatch, ...
            state["errors"][peer] = f"{type(e).__name__}: {e}"[:500]
            time.sleep(0.5)


def reachable():
    now = time.time()
    return [p for p in PEERS if now - state["last_seen"][p] < REACHABLE_WINDOW]


def barrier(v, timeout):
    """Wait until every reachable peer has pulled our db_version >= v, then pull
    from each reachable peer once. Returns who acked and whom we pulled from."""
    deadline = time.time() + timeout
    while True:
        peers = reachable()
        waiting = [p for p in peers if state["acked"][p] < v]
        if not waiting or time.time() > deadline:
            break
        time.sleep(0.01)
    pulled = []
    for p in peers:
        try:
            pull_once(p, 0.0)
            pulled.append(p)
        except Exception:
            pass
    return {"reachable": peers, "not_acked": waiting, "pulled": pulled}


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Headers and body go out in two writes; without TCP_NODELAY, Nagle plus
    # delayed ACKs add ~40 ms to every keep-alive request.
    disable_nagle_algorithm = True

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path == "/changes":
                since, peer = int(qs["since"]), qs.get("peer")
                if peer in state["acked"]:
                    state["acked"][peer] = max(state["acked"][peer], since)
                    state["last_seen"][peer] = time.time()
                deadline = time.time() + float(qs.get("wait", 0))
                with cond:
                    while True:
                        if paused:
                            raise RuntimeError("paused")
                        rows, wm, more = changes_since(since, qs["site"])
                        if rows or more or wm > since or time.time() >= deadline:
                            break
                        cond.wait(max(0.0, deadline - time.time()))
                self.reply(200, {"changes": [[enc(x) for x in r] for r in rows], "wm": wm, "more": more})
            elif u.path == "/status":
                with lock:
                    info = {"node": NODE, "site_id": site_hex, "paused": paused}
                    if conn is not None:
                        info["db_version"] = db_version()
                        info["wm"] = dict(conn.execute("SELECT peer, wm FROM _sync_peer").fetchall())
                info.update(state)
                info["reachable"] = reachable()
                self.reply(200, info)
            else:
                self.reply(404, {"error": "not found"})
        except Exception as e:
            self.reply(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        global paused
        try:
            req = self.body()
            if self.path == "/sql":
                self.reply(200, run_sql(req))
            elif self.path == "/barrier":
                self.reply(200, barrier(int(req["v"]), float(req.get("timeout", 5))))
            elif self.path == "/migrate":
                with lock:
                    paused = True
                    close_db()
                try:
                    p = subprocess.run(
                        ["alembic", "upgrade", req.get("target", "head")],
                        cwd="/app", capture_output=True, text=True,
                    )
                finally:
                    with lock:
                        open_db()
                        paused = False
                self.reply(200, {"rc": p.returncode, "out": p.stdout[-4000:], "err": p.stderr[-4000:]})
            elif self.path == "/backup":
                with lock:
                    t = time.time()
                    path = req.get("path", "/data/backup.db")
                    if os.path.exists(path):
                        os.remove(path)
                    conn.execute("VACUUM INTO ?", (path,))
                self.reply(200, {"path": path, "seconds": time.time() - t, "bytes": os.path.getsize(path)})
            else:
                self.reply(404, {"error": "not found"})
        except Exception as e:
            self.reply(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    open_db()
    for p in PEERS:
        threading.Thread(target=peer_loop, args=(p,), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", 8080), H)
    srv.daemon_threads = True
    print(f"{NODE} site={site_hex} peers={PEERS}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
