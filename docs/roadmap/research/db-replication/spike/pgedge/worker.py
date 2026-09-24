"""S3 job worker. One process per node.

Claim technique (both modes):
    UPDATE jobs SET lease_owner = me, lease_until = now() + 30 s
     WHERE id = (SELECT id FROM jobs
                  WHERE done_at IS NULL AND run_at <= now()
                    AND (lease_until IS NULL OR lease_until < now())
                  ORDER BY run_at, id LIMIT 1
                  FOR UPDATE SKIP LOCKED)
    RETURNING id
SKIP LOCKED only serialises workers on the *same* node: Spock replicates
asynchronously, so row locks are not visible on other nodes.

mode=naive   fire right after the claim commits.
mode=settle  after the claim commits, wait --settle-ms (well above the
             observed replication lag), re-read the row and fire only if
             lease_owner is still me. Concurrent claims of one row on two nodes
             are an update/update conflict that Spock resolves last-update-wins
             on every node, so exactly one claimant keeps the lease once
             replication has delivered both updates.
"""
import argparse
import json
import os
import time

import psycopg

CLAIM = """
UPDATE jobs SET lease_owner = %(w)s, lease_until = now() + interval '30 seconds'
 WHERE id = (SELECT id FROM jobs
              WHERE done_at IS NULL AND run_at <= now()
                AND (lease_until IS NULL OR lease_until < now())
              ORDER BY run_at, id LIMIT 1
              FOR UPDATE SKIP LOCKED)
RETURNING id
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stop-file", required=True)
    ap.add_argument("--mode", choices=["naive", "settle"], default="naive")
    ap.add_argument("--settle-ms", type=int, default=500)
    ap.add_argument("--work-ms", type=int, default=50)
    a = ap.parse_args()
    dsn = f"host=127.0.0.1 port={a.port} dbname=titan user=postgres password=spike connect_timeout=5"
    stats = {"claimed": 0, "fired": 0, "lost_race_after_settle": 0, "done_update_missed": 0,
             "idle_polls": 0, "errors": []}
    conn = None
    with open(a.out, "a", buffering=1) as f:
        while not os.path.exists(a.stop_file):
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(dsn, autocommit=True)
                row = conn.execute(CLAIM, {"w": a.name}).fetchone()
                if row is None:
                    stats["idle_polls"] += 1
                    time.sleep(0.2)
                    continue
                jid = row[0]
                stats["claimed"] += 1
                if a.mode == "settle":
                    time.sleep(a.settle_ms / 1000)
                    owner, done = conn.execute(
                        "SELECT lease_owner, done_at FROM jobs WHERE id = %s", (jid,)).fetchone()
                    if owner != a.name or done is not None:
                        stats["lost_race_after_settle"] += 1
                        continue
                time.sleep(a.work_ms / 1000)  # the "work" of sending a reminder
                f.write(f"{jid},{a.name}\n")
                f.flush()
                os.fsync(f.fileno())
                stats["fired"] += 1
                cur = conn.execute(
                    "UPDATE jobs SET done_at = now() WHERE id = %s AND lease_owner = %s AND done_at IS NULL",
                    (jid, a.name))
                if cur.rowcount == 0:
                    stats["done_update_missed"] += 1
            except Exception as e:  # noqa: BLE001 - record and keep going
                stats["errors"].append(f"{time.strftime('%H:%M:%S')} {type(e).__name__}: {str(e).strip()[:200]}")
                try:
                    conn and conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(0.5)
    with open(a.out + ".stats.json", "w") as f:
        json.dump(stats, f, indent=1)


if __name__ == "__main__":
    main()
