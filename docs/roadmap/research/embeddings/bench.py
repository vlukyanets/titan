"""Measure one embeddings server against the benchmark set (ADR 0014).

Usage: python bench.py URL MODEL QUERY_PREFIX DOCUMENT_PREFIX
Prints one JSON line: recall@5 and MRR (all and cross-language), query latency,
indexing speed, and the distances that set the cut-off.
"""

import json
import math
import statistics
import sys
import time

import httpx
from data import ITEMS, QUERIES, UNRELATED, language

url, model, query_prefix, document_prefix = sys.argv[1:5]
client = httpx.Client(timeout=600)


def embed(texts: list[str], prefix: str) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), 32):
        batch = [prefix + text for text in texts[start : start + 32]]
        response = client.post(f"{url}/v1/embeddings", json={"model": model, "input": batch})
        response.raise_for_status()
        vectors += [
            d["embedding"] for d in sorted(response.json()["data"], key=lambda d: d["index"])
        ]
    return vectors


def distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return 1 - dot / math.sqrt(sum(x * x for x in a)) / math.sqrt(sum(x * x for x in b))


names = list(ITEMS)
embed(["warm up"], document_prefix)
documents = dict(zip(names, embed([ITEMS[n] for n in names], document_prefix), strict=True))

# Indexing: 32 chunks of about 1000 characters, as the worker sends them.
long_chunk = " ".join(ITEMS.values())[:1000]
started = time.perf_counter()
embed([f"{i} {long_chunk}" for i in range(32)], document_prefix)
chunks_per_second = 32 / (time.perf_counter() - started)

latencies, ranks, cross_ranks, right_distances = [], [], [], []
for query, query_language, want in QUERIES:
    started = time.perf_counter()
    (vector,) = embed([query], query_prefix)
    latencies.append((time.perf_counter() - started) * 1000)
    ordered = sorted(names, key=lambda n: distance(vector, documents[n]))
    rank = ordered.index(want) + 1
    ranks.append(rank)
    if language(want) != query_language:
        cross_ranks.append(rank)
    right_distances.append(distance(vector, documents[want]))

nearest_unrelated = []
for query in UNRELATED:
    (vector,) = embed([query], query_prefix)
    nearest_unrelated.append(min(distance(vector, documents[n]) for n in names))


def recall(found: list[int]) -> float:
    return sum(r <= 5 for r in found) / len(found)


def mrr(found: list[int]) -> float:
    return sum(1 / r for r in found) / len(found)


latencies.sort()
print(
    json.dumps(
        {
            "model": model,
            "recall5": round(recall(ranks), 3),
            "mrr": round(mrr(ranks), 3),
            "recall5_cross": round(recall(cross_ranks), 3),
            "mrr_cross": round(mrr(cross_ranks), 3),
            "latency_median_ms": round(statistics.median(latencies)),
            "latency_p95_ms": round(latencies[int(len(latencies) * 0.95) - 1]),
            "chunks_per_second": round(chunks_per_second, 2),
            # A cut-off between these keeps most matches and drops unrelated queries.
            "right_distance_p90": round(
                sorted(right_distances)[int(len(right_distances) * 0.9)], 3
            ),
            "right_distance_max": round(max(right_distances), 3),
            "unrelated_nearest_min": round(min(nearest_unrelated), 3),
            "right_distances": sorted(round(d, 3) for d in right_distances),
            "unrelated_nearest": sorted(round(d, 3) for d in nearest_unrelated),
            "queries": len(ranks),
            "cross_queries": len(cross_ranks),
        }
    )
)
