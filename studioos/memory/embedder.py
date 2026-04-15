"""Embedder protocol + implementations.

FakeEmbedder gives deterministic vectors derived from text content (good for
tests and dev environments without API keys). OpenAIEmbedder calls OpenAI's
text-embedding-3-small. Other providers (Voyage, Cohere, local sentence-
transformers) can be added by implementing the same protocol.
"""
from __future__ import annotations

import hashlib
import math
from typing import Protocol

import httpx

from studioos.config import settings
from studioos.logging import get_logger

EMBEDDING_DIM = 1536

log = get_logger(__name__)


class Embedder(Protocol):
    """Async embedder contract."""

    dim: int

    async def embed(self, text: str) -> list[float]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder:
    """Deterministic, offline embedder for tests and dev.

    Hashes the input into a 1536-dim float vector. Same input → same vector,
    similar inputs are not necessarily similar (no semantic meaning), but it
    satisfies the protocol and lets the rest of the stack run without an API
    key. Good enough for round-trip tests.
    """

    dim = EMBEDDING_DIM

    async def embed(self, text: str) -> list[float]:
        return self._sync_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._sync_embed(t) for t in texts]

    def _sync_embed(self, text: str) -> list[float]:
        # Expand a sha512 digest into 1536 floats.
        seed = hashlib.sha512(text.encode("utf-8")).digest()
        # Each digest is 64 bytes; we need 1536 floats. Repeat with index.
        out: list[float] = []
        i = 0
        while len(out) < self.dim:
            chunk = hashlib.sha512(seed + i.to_bytes(4, "big")).digest()
            for b in chunk:
                if len(out) >= self.dim:
                    break
                out.append((b - 127.5) / 127.5)
            i += 1
        # L2-normalize to unit vector for cosine similarity sanity
        norm = math.sqrt(sum(x * x for x in out))
        return [x / norm for x in out] if norm else out


class OpenAIEmbedder:
    """OpenAI text-embedding-3-small (1536 dims)."""

    dim = EMBEDDING_DIM
    model = "text-embedding-3-small"

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)

    async def embed(self, text: str) -> list[float]:
        result = await self.embed_batch([text])
        return result[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": texts},
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]

    async def close(self) -> None:
        await self._client.aclose()


_singleton: Embedder | None = None


def get_embedder() -> Embedder:
    """Return the configured embedder.

    Picks OpenAIEmbedder if STUDIOOS_OPENAI_API_KEY is set, otherwise the
    FakeEmbedder (zero-config dev/test).
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    if settings.openai_api_key:
        log.info("embedder.openai")
        _singleton = OpenAIEmbedder(api_key=settings.openai_api_key)
    else:
        log.info("embedder.fake")
        _singleton = FakeEmbedder()
    return _singleton


def reset_embedder() -> None:
    """Test helper — drop the cached singleton."""
    global _singleton
    _singleton = None
