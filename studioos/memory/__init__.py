"""Memory layer — embedder protocol + semantic store."""
from __future__ import annotations

from studioos.memory.embedder import (
    Embedder,
    FakeEmbedder,
    OpenAIEmbedder,
    get_embedder,
)
from studioos.memory.store import (
    MemorySearchResult,
    record_episodic,
    record_memory,
    search_memory,
)

__all__ = [
    "Embedder",
    "FakeEmbedder",
    "OpenAIEmbedder",
    "get_embedder",
    "MemorySearchResult",
    "record_memory",
    "record_episodic",
    "search_memory",
]
