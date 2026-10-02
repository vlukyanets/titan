# Plan: M2 notes and memory

Implements the "Notes, memory and semantic search" item of
[M2](../milestones.md) and the [notes spec](../../spec/domains/notes-memory.md),
except what waits for the embedding model
([open question 3](../open-questions.md)).

Branch `feature/m2-notes`, stacked on `fix/m2-task-search-case`.

- `titan.domains.notes`: `notes`, `note_shares` and `memories` tables, the
  service with the sharing rules, word search under the `pg_unicode_fast`
  collation, and `remember`, which confirms a known statement instead of
  storing it twice.
- REST API under `/api/v1/notes` and `/api/v1/memories`.
- Embeddings, chunking and semantic search need the model and the
  `embeddings` image. Picking the model moved here from M0: multilingual
  (English, Russian and Ukrainian), CPU-friendly, benchmarked on a small
  note set for quality, latency and memory, decided in an ADR.

Tasks:

- [x] Spec and this plan.
- [x] Tables, migration and service for notes and memories.
- [x] Notes and memories API, OpenAPI regenerated, docs updated.
- [ ] Embedding model: shortlist, benchmark on a node, ADR.
- [ ] The `embeddings` image and its Compose service.
- [ ] Embeddings and semantic search.
- [x] Agent tools for notes and memory (`feature/m2-notes-agent-tools`).
