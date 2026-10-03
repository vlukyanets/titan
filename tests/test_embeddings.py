"""The embeddings client against a mocked server, and chunking."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from titan.domains.notes.index import CHUNK_CHARS, OVERLAP, note_chunks, split
from titan.embeddings import BATCH, EmbeddingsError, HttpEmbedder, from_settings
from titan.settings import Settings

URL = "postgresql+psycopg://user:pw@db/titan"
MODEL = "Snowflake/snowflake-arctic-embed-m-v2.0"


def server(model: str = MODEL, status: int = 200) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"url": str(request.url), **body})
        # Out of order on purpose: the client sorts by index.
        data = [{"index": i, "embedding": [float(i), 1.0]} for i in range(len(body["input"]))][::-1]
        return httpx.Response(status, json={"model": model, "data": data})

    return httpx.AsyncClient(transport=httpx.MockTransport(answer)), seen


async def test_texts_get_their_prefix_and_vectors_come_back_in_order() -> None:
    client, seen = server()
    embedder = HttpEmbedder(
        client, "http://embeddings/", MODEL, query_prefix="query: ", document_prefix="passage: "
    )
    texts = [f"t{i}" for i in range(BATCH + 3)]
    vectors = await embedder.embed(texts)
    assert vectors == [[float(i), 1.0] for i in range(BATCH)] + [[float(i), 1.0] for i in range(3)]
    assert [len(call["input"]) for call in seen] == [BATCH, 3]
    assert seen[0]["url"] == "http://embeddings/v1/embeddings"
    assert seen[0]["model"] == MODEL
    assert seen[0]["input"][0] == "passage: t0"
    await embedder.embed(["milk"], query=True)
    assert seen[-1]["input"] == ["query: milk"]


async def test_another_model_or_a_failure_is_refused() -> None:
    client, _ = server(model="someone/else")
    with pytest.raises(EmbeddingsError, match="someone/else"):
        await HttpEmbedder(client, "http://embeddings", MODEL).embed(["a"])
    client, _ = server(status=503)
    with pytest.raises(EmbeddingsError, match="HTTPStatusError"):
        await HttpEmbedder(client, "http://embeddings", MODEL).embed(["a"])

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": MODEL, "data": [{"index": 0}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(garbage))
    with pytest.raises(EmbeddingsError, match="unexpected shape"):
        await HttpEmbedder(client, "http://embeddings", MODEL).embed(["a"])


def test_the_url_turns_embeddings_on() -> None:
    client = httpx.AsyncClient()
    assert from_settings(Settings(database_url=URL), client) is None
    assert from_settings(Settings(database_url=URL, embeddings_url=" "), client) is None
    embedder = from_settings(Settings(database_url=URL, embeddings_url="http://embeddings"), client)
    assert embedder is not None
    assert embedder.model == MODEL
    with pytest.raises(ValidationError):
        Settings(database_url=URL, embeddings_url="ftp://embeddings")


def test_short_text_is_one_chunk_with_the_title() -> None:
    assert note_chunks("Shopping", "milk, bread") == ["Shopping\n\nmilk, bread"]
    assert note_chunks("Only a title", "  ") == ["Only a title"]
    assert note_chunks("", "just a body") == ["just a body"]


def test_long_text_is_cut_at_paragraphs_then_sentences_with_overlap() -> None:
    first = "word " * 150  # 750 characters
    text = first.strip() + "\n\n" + "Second paragraph. " * 60
    pieces = split(text)
    assert pieces[0] == first.strip()
    assert all(len(piece) <= CHUNK_CHARS for piece in pieces)
    # The second piece starts inside the end of the first.
    assert pieces[1].startswith("word")
    # Inside one paragraph the cut falls after a sentence.
    assert all(piece.endswith(".") for piece in pieces[1:-1])
    # Nothing is lost: every sentence is in some piece.
    assert sum(piece.count("Second paragraph.") for piece in pieces) >= 60


def test_text_without_breaks_is_cut_hard_and_always_advances() -> None:
    pieces = split("x" * (CHUNK_CHARS * 3))
    assert [len(piece) for piece in pieces] == [CHUNK_CHARS] * 3
    assert len(split("y " * 5000)) < 5000 * 2 // (CHUNK_CHARS - OVERLAP) + 2
