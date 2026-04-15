"""Bus factory — selects backend from settings."""
from __future__ import annotations

from studioos.config import settings
from studioos.logging import get_logger

from .base import BusBackend
from .inproc import get_inproc_bus, reset_inproc_buses

log = get_logger(__name__)

_redis_singleton: BusBackend | None = None


def get_bus() -> BusBackend:
    global _redis_singleton
    backend = settings.bus_backend.lower()
    if backend == "redis":
        if _redis_singleton is None:
            from .redis_backend import RedisBus

            _redis_singleton = RedisBus(
                redis_url=settings.redis_url,
                stream=settings.bus_stream,
                dlq_stream=settings.bus_dlq_stream,
            )
            log.info("bus.redis", url=settings.redis_url, stream=settings.bus_stream)
        return _redis_singleton
    # default: inproc
    return get_inproc_bus(
        stream=settings.bus_stream, dlq_stream=settings.bus_dlq_stream
    )


def reset_bus() -> None:
    global _redis_singleton
    _redis_singleton = None
    reset_inproc_buses()
