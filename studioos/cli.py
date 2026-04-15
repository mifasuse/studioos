"""StudioOS CLI — init, trigger, inspect, serve."""
from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from studioos import __version__
from studioos.config import settings
from studioos.db import session_scope
from studioos.logging import configure_logging
from studioos.studios import seed_all

# Workflow imports must happen so they register themselves
from studioos import workflows  # noqa: F401
from studioos.events import schemas_test  # noqa: F401

app = typer.Typer(help="StudioOS control CLI")
console = Console()


@app.callback()
def main() -> None:
    configure_logging()


@app.command()
def version() -> None:
    """Print version."""
    console.print(f"StudioOS v{__version__}")


@app.command()
def init() -> None:
    """Seed studios from bundled YAML configs."""

    async def _run() -> None:
        async with session_scope() as session:
            count = await seed_all(session)
        console.print(f"[green]Seeded {count} studio(s)[/green]")

    asyncio.run(_run())


@app.command()
def trigger(
    agent_id: Annotated[str, typer.Argument(help="Agent id to trigger")],
    correlation_id: Annotated[
        str | None,
        typer.Option(help="Correlation id to attach (optional)"),
    ] = None,
    priority: Annotated[int, typer.Option(help="Run priority")] = 50,
) -> None:
    """Enqueue a pending run for an agent."""
    from studioos.runtime.triggers import create_pending_run

    async def _run() -> None:
        corr = UUID(correlation_id) if correlation_id else None
        async with session_scope() as session:
            run = await create_pending_run(
                session,
                agent_id=agent_id,
                trigger_type="manual",
                trigger_ref="cli",
                correlation_id=corr,
                priority=priority,
            )
            console.print(
                f"[green]Enqueued run {run.id} for agent {agent_id}[/green]"
            )
            console.print(f"correlation_id: {run.correlation_id}")

    asyncio.run(_run())


@app.command()
def runs(
    agent_id: Annotated[str | None, typer.Option(help="Filter by agent")] = None,
    correlation: Annotated[
        str | None, typer.Option(help="Filter by correlation id")
    ] = None,
    limit: Annotated[int, typer.Option(help="Limit")] = 20,
) -> None:
    """List recent runs."""
    from sqlalchemy import desc, select

    from studioos.models import AgentRun

    async def _run() -> None:
        async with session_scope() as session:
            stmt = select(AgentRun).order_by(desc(AgentRun.created_at)).limit(limit)
            if agent_id:
                stmt = stmt.where(AgentRun.agent_id == agent_id)
            if correlation:
                stmt = stmt.where(AgentRun.correlation_id == UUID(correlation))
            rows = (await session.execute(stmt)).scalars().all()

        table = Table(title=f"Runs (latest {len(rows)})")
        table.add_column("Run ID", style="cyan", no_wrap=False)
        table.add_column("Agent", style="magenta")
        table.add_column("State", style="yellow")
        table.add_column("Trigger")
        table.add_column("Summary")
        for r in rows:
            summary = ""
            if r.output_snapshot:
                summary = str(r.output_snapshot.get("summary") or "")[:80]
            table.add_row(
                str(r.id)[:8],
                r.agent_id,
                r.state,
                f"{r.trigger_type}:{(r.trigger_ref or '')[:12]}",
                summary,
            )
        console.print(table)

    asyncio.run(_run())


@app.command()
def events(
    correlation: Annotated[
        str | None, typer.Option(help="Filter by correlation id")
    ] = None,
    event_type: Annotated[
        str | None, typer.Option(help="Filter by event type")
    ] = None,
    limit: Annotated[int, typer.Option(help="Limit")] = 20,
) -> None:
    """List recent events."""
    from sqlalchemy import desc, select

    from studioos.models import Event

    async def _run() -> None:
        async with session_scope() as session:
            stmt = select(Event).order_by(desc(Event.recorded_at)).limit(limit)
            if correlation:
                stmt = stmt.where(Event.correlation_id == UUID(correlation))
            if event_type:
                stmt = stmt.where(Event.event_type == event_type)
            rows = (await session.execute(stmt)).scalars().all()

        table = Table(title=f"Events (latest {len(rows)})")
        table.add_column("Event", style="cyan")
        table.add_column("v", justify="right")
        table.add_column("Source", style="magenta")
        table.add_column("Published")
        table.add_column("Payload", style="dim")
        for e in rows:
            table.add_row(
                e.event_type,
                str(e.event_version),
                f"{e.source_type}:{e.source_id or ''}",
                "✓" if e.published_at else "…",
                str(e.payload)[:60],
            )
        console.print(table)

    asyncio.run(_run())


@app.command()
def inspect(
    correlation: Annotated[str, typer.Option(help="Correlation id to inspect")],
) -> None:
    """Show the full run + event chain for a correlation id."""
    from sqlalchemy import select

    from studioos.models import AgentRun, Event

    async def _run() -> None:
        cid = UUID(correlation)
        async with session_scope() as session:
            runs = (
                (
                    await session.execute(
                        select(AgentRun)
                        .where(AgentRun.correlation_id == cid)
                        .order_by(AgentRun.created_at)
                    )
                )
                .scalars()
                .all()
            )
            events = (
                (
                    await session.execute(
                        select(Event)
                        .where(Event.correlation_id == cid)
                        .order_by(Event.occurred_at)
                    )
                )
                .scalars()
                .all()
            )

        console.rule(f"[bold cyan]Correlation {cid}[/bold cyan]")

        run_table = Table(title=f"Runs ({len(runs)})")
        run_table.add_column("#", justify="right")
        run_table.add_column("Agent", style="magenta")
        run_table.add_column("State", style="yellow")
        run_table.add_column("Trigger")
        run_table.add_column("Started")
        run_table.add_column("Summary")
        for i, r in enumerate(runs, start=1):
            summary = ""
            if r.output_snapshot:
                summary = str(r.output_snapshot.get("summary") or "")[:60]
            run_table.add_row(
                str(i),
                r.agent_id,
                r.state,
                f"{r.trigger_type}:{(r.trigger_ref or '')[:12]}",
                r.started_at.strftime("%H:%M:%S") if r.started_at else "-",
                summary,
            )
        console.print(run_table)

        ev_table = Table(title=f"Events ({len(events)})")
        ev_table.add_column("#", justify="right")
        ev_table.add_column("Event", style="cyan")
        ev_table.add_column("Source", style="magenta")
        ev_table.add_column("Payload", style="dim")
        for i, e in enumerate(events, start=1):
            ev_table.add_row(
                str(i),
                e.event_type,
                f"{e.source_type}:{e.source_id or ''}",
                str(e.payload)[:60],
            )
        console.print(ev_table)

    asyncio.run(_run())


@app.command()
def runtime() -> None:
    """Start the runtime loop (scheduler + outbox)."""
    from studioos.runtime.loop import run_forever

    asyncio.run(run_forever())


@app.command()
def serve() -> None:
    """Start FastAPI + runtime loop together (production entrypoint)."""

    async def _run() -> None:
        import asyncio as _asyncio

        from studioos.runtime.loop import run_forever

        config = uvicorn.Config(
            "studioos.api:app",
            host=settings.api_host,
            port=settings.api_port,
            log_level=settings.log_level.lower(),
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        await _asyncio.gather(server.serve(), run_forever())

    asyncio.run(_run())


if __name__ == "__main__":
    app()
