# Plan: M2 notes and memory

Implements the "Notes, memory and semantic search" item of
[M2](../milestones.md) and the [notes spec](../../spec/domains/notes-memory.md).

Done on `feature/m2-notes` and `feature/m2-notes-agent-tools`:

- `titan.domains.notes`: `notes`, `note_shares` and `memories` tables, the
  service with the sharing rules, word search under the `pg_unicode_fast`
  collation, and `remember`, which confirms a known statement instead of
  storing it twice.
- REST API under `/api/v1/notes` and `/api/v1/memories`, and the agent tools.

## Semantic search

Follows [ADR 0014](../../adr/0014-embeddings-from-a-local-server.md). Branches,
each on the one before:

1. `spec/m2-embeddings`: the spec, ADR 0014 (Proposed) and this plan.
2. `research/m2-embedding-model`: the benchmark, its results in ADR 0014, and
   the ADR accepted. The benchmark code lives in
   `docs/roadmap/research/embeddings/` while the branch is open and is deleted
   before it merges; the ADR keeps the numbers.
3. `feature/m2-embeddings`: the Compose service, the settings, the table, the
   client and indexing.
4. `feature/m2-semantic-search`: hybrid search in the service, the API and the
   agent tools.

### Benchmark

- About 40 short notes and memories with obviously fake content, a third each
  in English, Russian and Ukrainian, and about 40 queries, each with the item
  it should find. At least half of the queries are in another language than
  their item, and some share no words with it.
- Every candidate from ADR 0014 runs under TEI on a test node (2 vCPU, 8 GB).
  Measured: recall@5 and MRR over all queries and over the cross-language
  ones, query latency (median and p95, one text), indexing speed (chunks per
  second in batches), and the container's memory after indexing.
- The default is the best recall whose query latency stays under about
  200 ms; the second preset is the best recall regardless of size, or, if that
  is the default already, the smallest model close to it. Both get their prefixes and a
  distance cut-off taken from the score spread on this set.

### Design

- **Settings** (`titan.settings`): `embeddings_url` (empty: off),
  `embeddings_model`, `embeddings_query_prefix`, `embeddings_document_prefix`,
  `embeddings_max_distance` (the cut-off, per model, given in the presets).
  `.env.example` lists both presets, with `TITAN_EMBEDDINGS_REVISION`, which
  only Compose reads.
- **Compose**: an `embeddings` service from TEI's CPU image pinned by version,
  `--model-id` and `--revision` from the settings, an `embeddings-models`
  volume for the weights, no memory limit for now, no published port. `api` and
  `worker` reach it at `http://embeddings` and do not wait for it: until the
  model is loaded, calls fail and fall back as described below.
- **Table** `embeddings`: `id`, `note_id` or `memory_id` (each a foreign key
  with `ON DELETE CASCADE`, exactly one set), `chunk_index`, `model`,
  `content_hash`, `vector` (pgvector `vector` without a dimension), unique on
  the item, model and chunk. The migration creates the `vector` extension.
  Spock: only the leased sweep writes these rows, so conflicts are rare and
  last commit wins is enough.
- **Client** (`titan.embeddings`, next to `titan.notify`): `httpx` against
  `/v1/embeddings`, batches, a short timeout for queries, refusing a response
  that names another model. A `Protocol` so tests pass a fake with fixed
  vectors.
- **Chunking**: title plus body, split at blank lines, then sentence ends, then
  spaces, into pieces of at most about 1000 characters with about 150
  characters of overlap; each chunk starts with the title. A memory is one
  chunk.
- **Indexing** is a worker sweep `embeddings`, registered only when the URL is
  set, under its own lease. One tick finds up to a batch of items without
  vectors of the current model or whose `content_hash` (an `md5` of the text,
  computed in SQL) differs, newest first, embeds them and replaces their rows
  in one transaction. An item changed meanwhile has a new hash and is picked up
  on the next tick. Once nothing is left for the current model, rows of other
  models are deleted. A server error skips the tick.
- **Search**: the query is embedded with the query prefix (failure or timeout:
  words only). Up to 50 nearest chunks by cosine distance among readable items
  and the current model, under the cut-off, best chunk per item. Word matches
  as today, newest first. The two lists are merged by reciprocal rank fusion
  (k = 60). Used by `NotesService.notes` and `MemoryService.memories` when the
  query has text, so the API and `search_notes` and `recall` follow.

Tasks:

- [x] Spec and this plan.
- [x] Tables, migration and service for notes and memories.
- [x] Notes and memories API, OpenAPI regenerated, docs updated.
- [x] Spec, ADR 0014 (Proposed) and this plan (`spec/m2-embeddings`).
- [ ] Benchmark on a node, presets chosen, ADR 0014 accepted. Postponed:
      until then the default is `intfloat/multilingual-e5-base`, the
      mid-size candidate with good cross-language retrieval.
- [x] Compose service, settings, `embeddings` table and migration.
- [x] Client, chunking and the indexing sweep.
- [ ] Hybrid search in notes and memories, API rules for `q`, OpenAPI
      regenerated.
- [ ] Docs: architecture overview (container, sweep), CLAUDE.md, the client
      repositories' notes on search order.
- [ ] Live check on a node: cross-language search in all three languages.
- [x] Agent tools for notes and memory (`feature/m2-notes-agent-tools`).
