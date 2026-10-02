# 0014. Embeddings from a local server

- Status: Proposed
- Date: 2026-10-02

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
- The default model and one alternative preset are picked by a benchmark on a
  node: retrieval across the three languages, query latency, indexing speed and
  memory. Results and the choice are recorded here when this ADR is accepted.
- No memory limit on the container for now. One is added, with the presets,
  once the cluster nodes and their other load are known.

## Consequences

- Changing the model, or the server, is a settings change followed by
  re-indexing in the background. No migration: the vector column has no fixed
  dimension.
- Without a fixed dimension pgvector cannot build an HNSW index, so search
  compares the query with every vector the user can read. For personal notes
  that is thousands of chunks and a few milliseconds. When chunks pass about
  100 000, add a partial index per model with a cast to its dimension.
- One more container per node, 0.5 to 2.5 GB of memory depending on the
  model. Nodes without it (empty URL) search by words.
- CI does not run a model: tests use a fake client with fixed vectors. The
  cross-language behaviour is checked live on a node.
