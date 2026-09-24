"""Run scenarios against one candidate: python -m spike.run <candidate> [s1 s2 ...]."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from spike.cluster import CANDIDATES, Faults
from spike.scenarios import SCENARIOS, prepare

RESULTS = Path(__file__).resolve().parent.parent / "results"


async def main(name: str, wanted: list[str]) -> None:
    c = CANDIDATES[name]
    faults = Faults(c)
    path = RESULTS / f"{name}.json"
    results = json.loads(path.read_text()) if path.exists() else {}
    for key in wanted or list(SCENARIOS):
        ready = await prepare(c, faults)
        print(f"[{name}] {key}: cluster ready after {ready}s", flush=True)
        t0 = time.monotonic()
        try:
            results[key] = await SCENARIOS[key](c, faults)
        except Exception as exc:  # noqa: BLE001 - keep going and record the failure
            results[key] = {"crashed": f"{type(exc).__name__}: {exc}"[:500]}
        finally:
            faults.heal()
            for node in (1, 2, 3):
                faults.unpause(node)
        results[key]["_elapsed_s"] = round(time.monotonic() - t0, 1)
        path.write_text(json.dumps(results, indent=2, default=str) + "\n")
        print(json.dumps({key: results[key]}, indent=2, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
