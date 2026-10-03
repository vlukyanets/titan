# Domain: notes, knowledge and memory

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

## Entities

| Entity | Key fields |
|---|---|
| `Note` | id, owner, title, body (Markdown), tags, shared_with, created_at, updated_at |
| `Memory` | id, owner, statement ("Anna is allergic to peanuts"), source (chat id or note id), confidence, created_at, last_confirmed_at |
| `Embedding` | id, note or memory, chunk_index, model, content_hash, vector |

- Notes are written by the user. Memories are short facts that the agent
  extracts from conversations and the user can review.
- Notes and memories are split into chunks and embedded by a local
  embeddings server ([ADR 0014](../../adr/0014-embeddings-from-a-local-server.md)).
  Vectors are stored in the main database
  ([ADR 0006](../../adr/0006-replicated-database-with-vectors.md)) and
  removed with their note or memory.

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

- **Words.** `q` matches notes whose title, body or tags contain every word of
  the query, also inside longer words, ignoring case in every alphabet:
  "молок" finds "Молоко". Up to 10 words count. Memories are matched the same
  way, in the statement.
- **Meaning.** When the embeddings server is set up, `q` also finds notes and
  memories close in meaning, in any language: "car service" finds "ТО
  автомобиля". Items too far from the query are left out, so an unrelated
  query finds nothing rather than everything.
- Both kinds of match are merged into one list, best first. An item found
  both ways ranks higher.
- Search sees only what the user can read: their notes, notes shared with
  them, and only their own memories.
- **Chunks.** A note is embedded in chunks of about 1000 characters, cut at
  paragraph or sentence ends where possible, each with the note's title. A
  memory is one chunk. A note ranks by its best chunk.
- **Indexing** runs in the background on the worker, a few seconds after a
  note or memory is written or changed. Writing never waits for it and never
  fails because of it.
- **Fallback.** While the server is down, not set up, or still indexing (for
  example after a model change), search falls back to words for what has no
  current vector.

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

`PATCH` changes only the fields it sends. With `q`, lists come best match
first and are not paged: `limit` caps them, and `before` together with `q`
answers `422`.

## Agent tools

| Tool | Domain | Action class | Undo |
|---|---|---|---|
| `search_notes` / `get_note` | notes | `read` | – |
| `create_note` | notes | `write-internal` | Deletes the note |
| `update_note` | notes | `write-internal` | Restores title, body, tags and readers |
| `delete_note` | notes | `destructive` | – |
| `recall` | memory | `read` | – |
| `remember` | memory | `write-internal` | Forgets a new memory, or restores the confirmed one |
| `revise_memory` | memory | `write-internal` | Restores the statement |
| `forget` | memory | `destructive` | – |
| Sharing a note, or changing one that is shared | notes | `external` | As above |

- `search_notes` and `recall` search as described in [Search](#search).
  `get_note` shows at most 20 000 characters of a body.
- `remember` stores a memory with its source: the current chat thread, or
  the note it was read in. Outside a chat it needs the note. The agent's
  confidence defaults to 0.8.
- Members are named by username, as `list_members` shows them.

## Acceptance criteria (v1)

- A user can write, tag, search and share notes.
- Asking the agent a question about something stored in notes or memory returns
  an answer grounded in the matching items, with links to them.
- The user can list, edit and delete every memory the agent has stored about
  them.
- Search and recall find matching notes and memories written in another
  language than the query (at least English, Russian and Ukrainian, see
  [Languages](../product.md#languages)).
- Memories are never used to answer another user unless the source is shared
  with that user.
