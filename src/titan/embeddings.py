"""Text embeddings from a local OpenAI-compatible server (ADR 0014).

Texts go only to the configured URL, which is the node's own `embeddings`
container. Vectors from another model than the configured one are refused, so
a misconfigured server cannot mix models in the database.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from titan.settings import Settings

# TEI's default --max-client-batch-size.
BATCH = 32
# A search waits this long for its query vector, then falls back to words.
QUERY_TIMEOUT = 3.0
# Indexing on two CPU cores: a full batch of long chunks takes seconds.
BATCH_TIMEOUT = 120.0


class EmbeddingsError(Exception):
    """The server is unreachable, failed, or answered something unusable."""


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    # Cosine distance beyond which a search match is dropped; it depends on the model.
    @property
    def max_distance(self) -> float: ...

    async def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        """One vector per text, in order. `query` picks the query prefix."""
        ...


class HttpEmbedder:
    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        model: str,
        *,
        query_prefix: str = "",
        document_prefix: str = "",
        max_distance: float = 1.0,
    ) -> None:
        self._client = client
        self._url = url.rstrip("/") + "/v1/embeddings"
        self._model = model
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._max_distance = max_distance

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_distance(self) -> float:
        return self._max_distance

    async def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        prefix = self._query_prefix if query else self._document_prefix
        timeout = QUERY_TIMEOUT if query else BATCH_TIMEOUT
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            batch = [prefix + text for text in texts[start : start + BATCH]]
            vectors += await self._batch(batch, timeout)
        return vectors

    async def _batch(self, texts: list[str], wait: float) -> list[list[float]]:
        try:
            response = await self._client.post(
                self._url, json={"model": self._model, "input": texts}, timeout=wait
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingsError(f"embeddings server failed: {type(exc).__name__}") from exc
        try:
            model = body.get("model")
            items = sorted(body["data"], key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in items]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingsError("embeddings server answered an unexpected shape") from exc
        if model != self._model:
            raise EmbeddingsError(f"embeddings server runs {model!r}, not {self._model!r}")
        if len(vectors) != len(texts):
            raise EmbeddingsError("embeddings server answered the wrong number of vectors")
        return vectors


def from_settings(settings: Settings, client: httpx.AsyncClient) -> HttpEmbedder | None:
    """The configured embedder, or None when semantic search is off."""
    if settings.embeddings_url is None:
        return None
    return HttpEmbedder(
        client,
        settings.embeddings_url,
        settings.embeddings_model,
        query_prefix=settings.embeddings_query_prefix,
        document_prefix=settings.embeddings_document_prefix,
        max_distance=settings.embeddings_max_distance,
    )
