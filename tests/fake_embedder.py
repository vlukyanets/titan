"""A stand-in for the embeddings server: fixed vectors, no model."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from titan.embeddings import EmbeddingsError

DIMENSION = 8


def hashed(text: str) -> list[float]:
    """A unit vector that depends only on the text."""
    digest = hashlib.sha256(text.encode()).digest()
    raw = [byte - 127.5 for byte in digest[:DIMENSION]]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


@dataclass
class FakeEmbedder:
    model: str = "fake/model-a"
    max_distance: float = 0.5
    # Texts (without prefixes) with chosen vectors; the rest get hashed ones.
    vectors: dict[str, list[float]] = field(default_factory=dict)
    failing: bool = False
    calls: list[tuple[list[str], bool]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        if self.failing:
            raise EmbeddingsError("embeddings server failed: ConnectError")
        self.calls.append((list(texts), query))
        return [self.vectors.get(text) or hashed(text) for text in texts]
