"""Top-level runtime loop — runs dispatcher + outbox publisher concurrently."""
from __future__ import annotations

import asyncio
import signal

from studioos.config import settings
from studioos.logging import configure_logging, get_logger
from studioos.runtime.consumer import consumer_loop
from studioos.runtime.dispatcher import dispatch_loop
from studioos.runtime.outbox import outbox_loop
from studioos.scheduler import scheduler_loop

# Ensure workflows + event schemas + tools are imported and registered
from studioos import workflows  # noqa: F401
from studioos.events import schemas_amz, schemas_app, schemas_test  # noqa: F401
from studioos.tools import builtin as _builtin_tools  # noqa: F401

log = get_logger(__name__)


async def run_forever() -> None:
    configure_logging()
    log.info("runtime.starting", env=settings.env)

    # Register any MCP HTTP servers configured in the environment.
    # Failure here must not block startup.
    try:
        from studioos.tools.mcp_http import register_mcp_http_servers

        n = await register_mcp_http_servers()
        if n:
            log.info("runtime.mcp_http_tools_registered", count=n)
    except Exception:
        log.exception("runtime.mcp_http_register_failed")

    try:
        from studioos.tools.mcp_stdio import register_mcp_stdio_servers

        n = await register_mcp_stdio_servers()
        if n:
            log.info("runtime.mcp_stdio_tools_registered", count=n)
    except Exception:
        log.exception("runtime.mcp_stdio_register_failed")

    stop_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        log.info("runtime.signal", signal=sig.name)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows fallback
            pass

    dispatcher_task = asyncio.create_task(
        dispatch_loop(stop_event, settings.scheduler_tick_seconds),
        name="dispatcher",
    )
    outbox_task = asyncio.create_task(
        outbox_loop(stop_event, settings.outbox_poll_seconds),
        name="outbox",
    )
    consumer_task = asyncio.create_task(
        consumer_loop(stop_event),
        name="consumer",
    )
    scheduler_task = asyncio.create_task(
        scheduler_loop(stop_event, settings.agent_scheduler_tick_seconds),
        name="scheduler",
    )

    try:
        await stop_event.wait()
    finally:
        log.info("runtime.stopping")
        await asyncio.gather(
            dispatcher_task,
            outbox_task,
            consumer_task,
            scheduler_task,
            return_exceptions=True,
        )
        try:
            from studioos.tools.mcp_stdio import shutdown_mcp_stdio_servers

            await shutdown_mcp_stdio_servers()
        except Exception:
            log.exception("runtime.mcp_stdio_shutdown_failed")
        log.info("runtime.stopped")


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
