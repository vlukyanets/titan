"""S3 job worker. One process per node.

Two claim techniques (there is no cross-node lock in a CRDT):

barrier  Conditional UPDATE of lease_owner/lease_until on the local node, then
         POST /barrier: wait until every reachable peer has pulled our write,
         pull once more from every reachable peer, and re-read the row. Fire
         only if lease_owner is still us after the merge. Concurrent claims are
         resolved by cr-sqlite's per-column rule (higher col_version, then the
         larger value), which is deterministic, so both nodes agree on one
         owner while they can reach each other. A partitioned node sees no
         reachable peers and proceeds alone (AP).

vote     Majority vote per job. The claiming worker's node inserts its vote
         (job_id, voter=<node>, candidate=<worker>); every node casts exactly
         one vote per job, for the first claim it sees (the node's post-merge
         hook). A worker fires a job once 2 of 3 votes name it. Each vote cell
         has a single writer, so CRDT merges cannot flip a vote (CP: a node
         without a majority cannot fire).
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now():
    return datetime.now(timezone.utc)


class Node:
    def __init__(self, url):
        self.c = httpx.Client(base_url=url, timeout=30)

    def sql(self, sql, params=()):
        r = self.c.post("/sql", json={"sql": sql, "params": list(params)})
        if r.status_code != 200:
            raise RuntimeError(r.text)
        return r.json()

    def barrier(self, v, timeout=5):
        return self.c.post("/barrier", json={"v": v, "timeout": timeout}).json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--mode", choices=["barrier", "vote"], required=True)
    ap.add_argument("--kind", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stopfile", required=True)
    ap.add_argument("--stats", required=True)
    ap.add_argument("--deadline", type=float, default=300)
    a = ap.parse_args()
    n = Node(a.url)
    st = {"fired": 0, "errors": 0, "error_samples": [], "lost_local": 0, "lost_after_merge": 0,
          "barrier_incomplete": 0, "vote_claims": 0, "vote_lost": 0, "vote_timeouts": 0}
    out = open(a.out, "a")
    t_end = time.time() + a.deadline
    pending = None  # vote mode: (job_id, claimed_at)

    def fire(job_id):
        out.write(f"{job_id},{a.name}\n")
        out.flush()
        os.fsync(out.fileno())
        st["fired"] += 1

    def undone():
        return n.sql("SELECT count(*) FROM jobs WHERE kind=? AND done_at IS NULL", [a.kind])["rows"][0][0]

    while time.time() < t_end:
        try:
            t = now()
            if a.mode == "barrier":
                r = n.sql(
                    "SELECT id FROM jobs WHERE kind=? AND done_at IS NULL AND run_at<=? "
                    "AND (lease_owner IS NULL OR lease_until < ?) ORDER BY random() LIMIT 1",
                    [a.kind, iso(t), iso(t)])["rows"]
                if not r:
                    if os.path.exists(a.stopfile) and undone() == 0:
                        break
                    time.sleep(0.1)
                    continue
                jid = r[0][0]
                u = n.sql(
                    "UPDATE jobs SET lease_owner=?, lease_until=? WHERE id=? AND done_at IS NULL "
                    "AND (lease_owner IS NULL OR lease_until < ?)",
                    [a.name, iso(t + timedelta(seconds=30)), jid, iso(t)])
                if u["rowcount"] != 1:
                    st["lost_local"] += 1
                    continue
                for _ in range(10):
                    b = n.barrier(u["db_version"], 5)
                    if not b["not_acked"]:
                        break
                    st["barrier_incomplete"] += 1
                row = n.sql("SELECT lease_owner, done_at FROM jobs WHERE id=?", [jid])["rows"][0]
                if row[0] == a.name and row[1] is None:
                    fire(jid)
                    n.sql("UPDATE jobs SET done_at=? WHERE id=?", [iso(now()), jid])
                else:
                    st["lost_after_merge"] += 1
            else:
                won = n.sql(
                    "SELECT j.id FROM jobs j WHERE j.kind=? AND j.done_at IS NULL AND "
                    "(SELECT count(*) FROM job_votes v WHERE v.job_id=j.id AND v.candidate=?) >= 2 LIMIT 1",
                    [a.kind, a.name])["rows"]
                if won:
                    jid = won[0][0]
                    fire(jid)
                    n.sql("UPDATE jobs SET done_at=?, lease_owner=?, lease_until=? WHERE id=?",
                          [iso(now()), a.name, iso(now() + timedelta(seconds=30)), jid])
                    if pending and pending[0] == jid:
                        pending = None
                    continue
                if pending:
                    jid, since = pending
                    lost = n.sql(
                        "SELECT count(*) FROM job_votes WHERE job_id=? AND candidate<>? "
                        "GROUP BY candidate HAVING count(*) >= 2", [jid, a.name])["rows"]
                    done = n.sql("SELECT done_at FROM jobs WHERE id=?", [jid])["rows"][0][0]
                    if lost or done:
                        st["vote_lost"] += 1
                        pending = None
                    elif time.time() - since > 10:
                        st["vote_timeouts"] += 1
                        pending = None
                    else:
                        time.sleep(0.02)
                        continue
                r = n.sql(
                    "SELECT id FROM jobs j WHERE kind=? AND done_at IS NULL AND run_at<=? AND NOT EXISTS "
                    "(SELECT 1 FROM job_votes v WHERE v.job_id=j.id AND v.voter=?) ORDER BY random() LIMIT 1",
                    [a.kind, iso(t), a.node])["rows"]
                if r:
                    jid = r[0][0]
                    u = n.sql("INSERT OR IGNORE INTO job_votes(job_id, voter, candidate, voted_at) VALUES (?,?,?,?)",
                              [jid, a.node, a.name, iso(t)])
                    if u["rowcount"] == 1:
                        st["vote_claims"] += 1
                        pending = (jid, time.time())
                    continue
                if os.path.exists(a.stopfile) and undone() == 0:
                    break
                time.sleep(0.1)
        except Exception as e:
            st["errors"] += 1
            if len(st["error_samples"]) < 5:
                st["error_samples"].append(f"{type(e).__name__}: {e}"[:300])
            time.sleep(0.2)
    st["timed_out"] = time.time() >= t_end
    with open(a.stats, "w") as f:
        json.dump(st, f)


if __name__ == "__main__":
    main()
