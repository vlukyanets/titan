# 0014. Embeddings from a local server

- Status: Accepted
- Date: 2026-10-03

## Context

Notes and memories need semantic search, and it must find items written in
another language than the query: at least English, Russian and Ukrainian
([notes spec](../spec/domains/notes-memory.md#acceptance-criteria-v1),
[Languages](../spec/product.md#languages)). Vectors live in the main database
with pgvector ([ADR 0006](0006-replicated-database-with-vectors.md)), and the
model runs in its own `embeddings` container so it can be sized, moved or
swapped ([overview](../architecture/overview.md#containers)).

Constraints:

- Texts never leave the node. The model runs locally; only its weights are
  downloaded, once.
- A node is about 2 vCPU and 8 GB of RAM. The model holds its memory for as
  long as the container runs. There is no memory limit for now: models of up to
  about 3 GB are candidates, and the benchmark shows what the extra size buys.
- Better multilingual models keep appearing, so changing the model must be a
  setting, not a code change. Vectors of different models cannot be compared,
  so a change means computing every vector again.
- Notes are written even when the model is down, and search still works then.

## Options

How to serve the model:

1. **Hugging Face Text Embeddings Inference (TEI)**, its CPU image. A ready
   HTTP server, Apache-2.0, with an OpenAI-compatible `/v1/embeddings`. It runs
   XLM-RoBERTa, GTE, Gemma3, Qwen3 and other families, so every candidate
   below runs unchanged, and a model is picked by one argument.
2. **Our own FastAPI service with ONNX Runtime (fastembed).** Full control,
   but a service to maintain, and fewer models than TEI. The one thing it would
   add, query and document prefixes, is easy in titan itself.
3. **Ollama.** Loads models on demand and unloads them when idle, with
   quantized weights. Unloading saves little for a model that serves every
   search, and the E5 and Arctic models are not in its library.

Which model, up to about 3 GB of memory:

| Model | Parameters | Dimension | Memory, approx. | Licence |
|---|---|---|---|---|
| `intfloat/multilingual-e5-small` | 118M | 384 | 0.5 GB | MIT |
| `intfloat/multilingual-e5-base` | 278M | 768 | 1.1 GB | MIT |
| `Snowflake/snowflake-arctic-embed-m-v2.0` | 305M | 768 | 1.2 GB | Apache-2.0 |
| `Snowflake/snowflake-arctic-embed-l-v2.0` | 568M | 1024 | 2.3 GB | Apache-2.0 |
| `BAAI/bge-m3` | 568M | 1024 | 2.3 GB | MIT |
| `Qwen/Qwen3-Embedding-0.6B` | 596M | 1024 | 2.4 GB | Apache-2.0 |

`google/embeddinggemma-300m` fits too, but its download is gated behind a
Hugging Face account and the Gemma terms.

## Decision

Option 1, with the model as a setting.

- titan speaks only the OpenAI-compatible `/v1/embeddings` protocol, so any
  server that offers it works (TEI, Ollama, llama.cpp, vLLM, Infinity).
  Settings: `TITAN_EMBEDDINGS_URL` (empty turns semantic search off),
  `TITAN_EMBEDDINGS_MODEL`, and the query and document prefixes the model
  expects, `TITAN_EMBEDDINGS_QUERY_PREFIX` and
  `TITAN_EMBEDDINGS_DOCUMENT_PREFIX`, and the distance beyond which a match is
  dropped, `TITAN_EMBEDDINGS_MAX_DISTANCE`, which also depends on the model.
- titan refuses vectors whose response names another model than
  `TITAN_EMBEDDINGS_MODEL`, so a misconfigured server cannot mix models.
- Compose runs TEI's CPU image, pinned by version, with the model pinned by
  revision. Weights are downloaded into a volume on the first start; after
  that the node needs no internet for embeddings.
- Every vector records its model. Search uses only vectors of the current
  model. After a change the worker computes all vectors again, and search
  falls back to words for what is not done yet.
- All nodes use the same model. Only the node holding the indexing lease
  computes vectors, and replication brings them to the others.
- Two presets, from the benchmark below. **Default:**
  `Snowflake/snowflake-arctic-embed-m-v2.0`, query prefix `query: `, no
  document prefix, cut-off 0.8: the best retrieval with queries under about
  200 ms. **Best retrieval:** `BAAI/bge-m3`, no prefixes, cut-off 0.45, for a
  node with memory and time to spare. Both are pinned by revision in
  `.env.example`.
- TEI runs with `--max-batch-tokens=2048`. These models accept 8192 tokens,
  and warming up at that length runs a node out of memory; chunks are about
  300 tokens, so nothing is lost.
- No memory limit on the container for now. One is added, with the presets,
  once the cluster nodes and their other load are known.

## Benchmark

Run on a test node (2 vCPU, 8 GB) with TEI `cpu-1.9.4`: 40 fake notes and
memories, a third each in English, Russian and Ukrainian, and 42 queries, 33
of them in another language than their item, plus 6 queries with no match.
Latency is one query; indexing is batches of 32 chunks of about 1000
characters; memory is the container's after the run.

| Model | Recall@5 (cross-language) | MRR (cross-language) | Query, median / p95 | Chunks/s | Memory |
|---|---|---|---|---|---|
| `multilingual-e5-small` | 0.86 (0.82) | 0.76 (0.69) | 29 / 50 ms | 1.82 | 1.0 GB |
| `multilingual-e5-base` | 0.81 (0.76) | 0.77 (0.70) | 108 / 194 ms | 0.45 | 1.7 GB |
| `snowflake-arctic-embed-m-v2.0` | 0.93 (0.91) | 0.88 (0.85) | 136 / 260 ms | 0.35 | 2.2 GB |
| `snowflake-arctic-embed-l-v2.0` | 1.00 (1.00) | 0.94 (0.92) | 389 / 618 ms | 0.14 | 3.4 GB |
| `bge-m3` | 1.00 (1.00) | 1.00 (1.00) | 323 / 501 ms | 0.14 | 3.3 GB |
| `Qwen3-Embedding-0.6B` | 0.95 (0.94) | 0.90 (0.88) | 1652 / 2152 ms | 0.07 | 3.5 GB |

- The E5 models need less memory than the estimates above but find the right
  item across languages clearly less often.
- `arctic-embed-l-v2.0` retrieves as well as `bge-m3` but is slower and
  larger; Qwen3 is too slow on CPU for search.
- **Cut-offs.** No cut-off separates every match from every unrelated query,
  so each preset trades them. For the default, 0.8 keeps 34 of the 42
  expected matches and lets 3 of the 6 unrelated queries find something,
  ranked low; for `bge-m3`, 0.45 keeps 35 of 42 with one unrelated hit. In a
  live check on a node with notes in all three languages, the default's
  matches lay between 0.57 and 0.80 and unrelated queries came no closer than
  0.87, so 0.8 found every match and nothing for unrelated queries.
- Indexing on two cores is slow: the default embeds about one chunk every
  three seconds, so a thousand notes take an hour or two in the background.

## Consequences

- Changing the model, or the server, is a settings change followed by
  re-indexing in the background. No migration: the vector column has no fixed
  dimension.
- Without a fixed dimension pgvector cannot build an HNSW index, so search
  compares the query with every vector the user can read. For personal notes
  that is thousands of chunks and a few milliseconds. When chunks pass about
  100 000, add a partial index per model with a cast to its dimension.
- One more container per node: 2.2 GB of memory with the default preset,
  3.3 GB with the other. Nodes without it (empty URL) search by words.
- CI does not run a model: tests use a fake client with fixed vectors. The
  cross-language behaviour is checked live on a node.
