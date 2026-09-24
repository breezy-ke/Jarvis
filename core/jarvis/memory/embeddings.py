"""Text embeddings for memory search. Always computed locally, never sent out.

* `FastEmbedEmbedder` is used in production: BAAI/bge-small-en-v1.5 via ONNX
  on the CPU (384 dimensions, about 130 MB, downloaded once).
* `HashEmbedder` is a deterministic, dependency-free fallback for tests and
  offline development. It captures word overlap, not meaning.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from itertools import pairwise
from typing import Any, Protocol

from jarvis.db.base import EMBEDDING_DIM

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    name = "hash"

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        tokens = _TOKEN_RE.findall(text.lower())
        features = tokens + [f"{a}_{b}" for a, b in pairwise(tokens)]
        vec = [0.0] * self.dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


class FastEmbedEmbedder:
    name = "fastembed:BAAI/bge-small-en-v1.5"

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self.dim = EMBEDDING_DIM
        self._model_name = model_name
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def _load(self) -> Any:
        async with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding  # imported lazily: heavy dependency

                self._model = await asyncio.to_thread(TextEmbedding, self._model_name)
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = await self._load()
        vectors = await asyncio.to_thread(lambda: list(model.embed(texts)))
        return [[float(x) for x in v] for v in vectors]


def create_embedder(kind: str) -> Embedder:
    if kind == "hash":
        return HashEmbedder()
    try:
        import fastembed  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "fastembed is not installed. Install the local-ml extra "
            "(`uv sync --extra local-ml`) or set JARVIS_EMBEDDER=hash."
        ) from exc
    return FastEmbedEmbedder()
