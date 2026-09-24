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
  Embeddings and semantic search arrive with the embedding model
  ([open question 3](../../roadmap/open-questions.md)); until then search
  matches words.

## Notes

- A title is at most 200 characters and may be empty; the body is Markdown of
  at most 100 000 characters. A note needs a title or a body.
- Tags follow the [task rules](tasks.md#entities): lower case, at most 20 per
  note, 32 characters each.
- A note is shared by listing users in `shared_with` (at most 50). They can
  read it and find it in search. Only its owner edits, shares or deletes it.
- Anything a user has no access to answers `404`, as if it did not exist; a
  shared user who tries to change a note gets `403`.
- Notes are listed most recently changed first. Lists carry an excerpt of the
  body, not the whole body.

## Search

- Until embeddings arrive, `q` matches notes whose title, body or tags
  contain every word of the query, also inside longer words, ignoring case in
  every alphabet: "молок" finds "Молоко". Up to 10 words count.
- Memories are searched the same way, in the statement.

## Memories

- A statement is 1–500 characters. Confidence is a number from 0 to 1.
- The source is `chat` (a chat thread), `note` or `user` (typed in by the
  user). A chat or note source is checked when the memory is stored: it must be
  the user's own thread, or a note they can read. It is only a link, so a
  memory outlives the thread or note it came from.
- When the agent stores a statement the user already has (ignoring case and
  spacing), the existing memory is confirmed instead of duplicated: its
  `last_confirmed_at` moves to now and its confidence rises to the higher of
  the two.
- A statement the user writes or edits has confidence 1 and counts as
  confirmed now.
- Memories belong to one user and are never shown, recalled or used for anyone
  else, even when their source is a shared note.

## API

| Call | Purpose |
|---|---|
| `GET /api/v1/notes` | Notes the caller owns or that are shared with them; filters: `q`, `tag`, `shared` (`mine` or `with_me`); paged with `before` and `limit` |
| `POST /api/v1/notes` | Create a note |
| `GET`, `PATCH`, `DELETE /api/v1/notes/{id}` | Read, change (title, body, tags, shared_with; owner only), delete (owner only) |
| `GET /api/v1/memories` | The caller's memories, newest first; filter `q`; paged with `before` and `limit` |
| `POST /api/v1/memories` | Add a memory by hand (source `user`) |
| `PATCH`, `DELETE /api/v1/memories/{id}` | Change the statement, forget it |

`PATCH` changes only the fields it sends.

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
