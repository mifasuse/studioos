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


class MiniMaxEmbedder:
    """MiniMax embo-01 embedder.

    API format differs from OpenAI:
      - URL: POST /v1/embeddings?GroupId=XXX
      - Body: {"model": "embo-01", "texts": [...], "type": "db"|"query"}
      - Response: {"vectors": [[...], ...]}
    """

    dim = EMBEDDING_DIM
    model = "embo-01"

    def __init__(self, api_key: str, group_id: str, base_url: str = "https://api.minimax.io/v1"):
        self.api_key = api_key
        self.group_id = group_id
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)
        self._dim_detected = False

    async def embed(self, text: str, embed_type: str = "db") -> list[float]:
        result = await self.embed_batch([text], embed_type=embed_type)
        return result[0]

    async def embed_batch(
        self, texts: list[str], embed_type: str = "db"
    ) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.post(
            f"{self.base_url}/embeddings?GroupId={self.group_id}",
            json={"model": self.model, "texts": texts, "type": embed_type},
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(
                f"minimax embedding error: {base_resp.get('status_msg', '?')}"
            )
        vectors = data.get("vectors") or []
        if not vectors:
            raise RuntimeError("minimax embedding returned empty vectors")
        # Auto-detect dimension on first successful call
        if not self._dim_detected and vectors:
            actual_dim = len(vectors[0])
            if actual_dim != self.dim:
                log.warning(
                    "embedder.minimax.dim_mismatch",
                    expected=self.dim,
                    actual=actual_dim,
                )
            self._dim_detected = True
        return vectors

    async def close(self) -> None:
        await self._client.aclose()


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
        # Retry on 429 (rate limit) with exponential backoff
        import asyncio as _asyncio
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": texts},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                if resp.status_code == 429:
                    # Respect Retry-After header, else exponential backoff
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else (2 ** attempt)
                    await _asyncio.sleep(min(wait, 10.0))
                    continue
                resp.raise_for_status()
                data = resp.json()
                return [item["embedding"] for item in data["data"]]
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code == 429 and attempt < 2:
                    await _asyncio.sleep(2 ** attempt)
                    continue
                raise
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < 2:
                    await _asyncio.sleep(2 ** attempt)
                    continue
                raise
        if last_exc:
            raise last_exc
        return []

    async def close(self) -> None:
        await self._client.aclose()


_singleton: Embedder | None = None


def get_embedder() -> Embedder:
    """Return the configured embedder.

    Priority:
      1. MiniMax (if MINIMAX_API_KEY + MINIMAX_GROUP_ID set)
      2. OpenAI (if OPENAI_API_KEY set)
      3. FakeEmbedder (zero-config dev/test)
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
