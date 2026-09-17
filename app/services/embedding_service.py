"""Semantic search over the merchant catalog.

Embeddings come from whichever `AIProvider` is configured (Bedrock Titan/Cohere
in AWS, the deterministic local provider offline). Vectors are cached by content
hash so repeated catalog searches do not re-invoke the model - the same cache
interface is satisfied by the in-process dict, a JSON file on disk, or
ElastiCache/Redis if this is ever scaled out.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from pathlib import Path
from typing import Iterable, Protocol

from app.core.exceptions import AIProviderError
from app.core.logging import get_logger
from app.services.ai_provider import AIProvider

logger = get_logger(__name__)


class EmbeddingCache(Protocol):
    def get(self, key: str) -> list[float] | None: ...

    def set(self, key: str, vector: list[float]) -> None: ...


class InMemoryEmbeddingCache:
    def __init__(self) -> None:
        self._data: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> list[float] | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, vector: list[float]) -> None:
        with self._lock:
            self._data[key] = vector


class JsonFileEmbeddingCache(InMemoryEmbeddingCache):
    """Persisted cache so restarts do not re-pay for embeddings."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = Path(path)
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Embedding cache unreadable, starting fresh")
                self._data = {}

    def set(self, key: str, vector: list[float]) -> None:
        super().set(key, vector)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data), encoding="utf-8")
        except OSError:  # pragma: no cover - disk issues must not break search
            logger.warning("Could not persist embedding cache")


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


class EmbeddingService:
    """Embed-and-rank helper shared by the merchant catalog."""

    def __init__(self, provider: AIProvider, cache: EmbeddingCache | None = None) -> None:
        self._provider = provider
        self._cache = cache or InMemoryEmbeddingCache()

    def _key(self, text: str) -> str:
        material = f"{self._provider.embedding_model_id}:{text}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        texts = list(texts)
        results: list[list[float] | None] = [None] * len(texts)
        pending: list[tuple[int, str]] = []
        for index, text in enumerate(texts):
            cached = self._cache.get(self._key(text))
            if cached is not None:
                results[index] = cached
            else:
                pending.append((index, text))

        if pending:
            vectors = self._provider.embed([text for _, text in pending])
            if len(vectors) != len(pending):
                raise AIProviderError(
                    "Embedding provider returned a mismatched number of vectors",
                    details={"expected": len(pending), "received": len(vectors)},
                )
            for (index, text), vector in zip(pending, vectors):
                self._cache.set(self._key(text), vector)
                results[index] = vector
            logger.debug(
                "Embeddings generated",
                extra={"generated": len(pending), "cached": len(texts) - len(pending)},
            )
        return [vector or [] for vector in results]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def rank(
        self, query: str, documents: dict[str, str], *, top_k: int = 5
    ) -> list[tuple[str, float]]:
        """Return `(document_id, score)` sorted by descending similarity."""
        if not documents:
            return []
        ids = list(documents.keys())
        vectors = self.embed_documents([documents[i] for i in ids])
        query_vector = self.embed_query(query)
        scored = [
            (doc_id, cosine_similarity(query_vector, vector))
            for doc_id, vector in zip(ids, vectors)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]
