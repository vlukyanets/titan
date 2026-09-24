# Domain: notes, knowledge and memory

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

## Entities

| Entity | Key fields |
|---|---|
| `Note` | id, owner, title, body (Markdown), tags, shared_with, created_at, updated_at |
| `Memory` | id, owner, statement ("Anna is allergic to peanuts"), source (chat id or note id), confidence, created_at, last_confirmed_at |
| `Embedding` | id, entity_type, entity_id, chunk_index, vector, model |

- Notes are written by the user. Memories are short facts that the agent
  extracts from conversations and the user can review.
- Notes and memories are split into chunks and embedded by the local
  embeddings service. Vectors are stored in the main database
  ([ADR 0006](../../adr/0006-replicated-database-with-vectors.md)).

## Agent tools

| Tool | Action class |
|---|---|
| `notes.search` (semantic + keyword) / `notes.get` / `memory.recall` | `read` |
| `notes.create` / `notes.update` / `memory.remember` / `memory.revise` | `write-internal` |
| `notes.delete` / `memory.forget` | `destructive` |

## Acceptance criteria (v1)

- A user can write, tag, search and share notes.
- Asking the agent a question about something stored in notes or memory returns
  an answer grounded in the matching items, with links to them.
- The user can list, edit and delete every memory the agent has stored about
  them.
- Memories are never used to answer another user unless the source is shared
  with that user.
