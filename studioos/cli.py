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
from studioos.events import schemas_amz, schemas_app, schemas_test  # noqa: F401
from studioos.tools import builtin as _builtin_tools  # noqa: F401

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
def memory(
    query: Annotated[str | None, typer.Option(help="Semantic search query")] = None,
    agent: Annotated[str | None, typer.Option(help="Filter by agent_id")] = None,
    studio: Annotated[str | None, typer.Option(help="Filter by studio_id")] = None,
    limit: Annotated[int, typer.Option(help="Max results")] = 10,
) -> None:
    """List or search semantic memories."""
    from sqlalchemy import desc, select

    from studioos.memory.store import search_memory
    from studioos.models import MemorySemantic

    async def _run() -> None:
        async with session_scope() as session:
            if query:
                results = await search_memory(
                    session,
                    query=query,
                    agent_id=agent,
                    studio_id=studio,
                    limit=limit,
                )
                table = Table(title=f"Memory search: {query!r}")
                table.add_column("Score", style="dim", justify="right")
                table.add_column("Agent", style="magenta")
                table.add_column("Tags", style="cyan")
                table.add_column("Content")
                for r in results:
                    table.add_row(
                        f"{1 - r.distance:.3f}",
                        agent or "",
                        ",".join(r.tags or []),
                        r.content[:80],
                    )
                console.print(table)
                return
            stmt = select(MemorySemantic).order_by(
                desc(MemorySemantic.created_at)
            ).limit(limit)
            if agent:
                stmt = stmt.where(MemorySemantic.agent_id == agent)
            if studio:
                stmt = stmt.where(MemorySemantic.studio_id == studio)
            rows = (await session.execute(stmt)).scalars().all()
            table = Table(title=f"Recent memories ({len(rows)})")
            table.add_column("When", style="dim")
            table.add_column("Agent", style="magenta")
            table.add_column("Tags", style="cyan")
            table.add_column("Content")
            for r in rows:
                table.add_row(
                    r.created_at.strftime("%H:%M:%S"),
                    r.agent_id or "",
                    ",".join(r.tags or []),
                    r.content[:80],
                )
            console.print(table)

    asyncio.run(_run())


@app.command()
def kpi(
    studio: Annotated[str | None, typer.Option(help="Filter by studio_id")] = None,
    agent: Annotated[str | None, typer.Option(help="Filter by agent_id")] = None,
) -> None:
    """Show KPI state for a scope (target vs current + gap)."""
    from studioos.kpi.store import get_current_state

    async def _run() -> None:
        async with session_scope() as session:
            states = await get_current_state(
                session, studio_id=studio, agent_id=agent
            )
        if not states:
            console.print("[dim]No KPI targets defined for this scope[/dim]")
            return
        table = Table(title="KPI State")
        table.add_column("Name", style="cyan")
        table.add_column("Target", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("Direction", style="dim")
        table.add_column("Reached")
        table.add_column("Gap", justify="right")
        for s in states:
            reached = ""
            gap_str = ""
            if s.gap is not None:
                reached = "✓" if s.gap.reached else "—"
                gap_str = str(s.gap.delta)
            table.add_row(
                s.display_name or s.name,
                str(s.target) if s.target is not None else "-",
                str(s.current) if s.current is not None else "-",
                s.direction,
                reached,
                gap_str,
            )
        console.print(table)

    asyncio.run(_run())


@app.command()
def tools() -> None:
    """List registered tools."""
    from studioos.tools import list_tools

    all_tools = list_tools()
    if not all_tools:
        console.print("[dim]No tools registered[/dim]")
        return
    table = Table(title=f"Registered tools ({len(all_tools)})")
    table.add_column("Name", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("Network", justify="center")
    table.add_column("Description")
    for t in all_tools:
        table.add_row(
            t.name,
            t.category,
            "✓" if t.requires_network else "",
            t.description[:60],
        )
    console.print(table)


@app.command("tool-calls")
def tool_calls_cmd(
    tool: Annotated[str | None, typer.Option(help="Filter by tool name")] = None,
    agent: Annotated[str | None, typer.Option(help="Filter by agent_id")] = None,
    limit: Annotated[int, typer.Option(help="Limit")] = 20,
) -> None:
    """Show recent tool invocations."""
    from sqlalchemy import desc, select

    from studioos.models import ToolCall

    async def _run() -> None:
        async with session_scope() as session:
            stmt = (
                select(ToolCall).order_by(desc(ToolCall.called_at)).limit(limit)
            )
            if tool:
                stmt = stmt.where(ToolCall.tool_name == tool)
            if agent:
                stmt = stmt.where(ToolCall.agent_id == agent)
            rows = (await session.execute(stmt)).scalars().all()

        table = Table(title=f"Tool calls (latest {len(rows)})")
        table.add_column("When", style="dim")
        table.add_column("Tool", style="cyan")
        table.add_column("Agent", style="magenta")
        table.add_column("Status")
        table.add_column("ms", justify="right")
        table.add_column("Error", style="red")
        for r in rows:
            status_style = "green" if r.status == "ok" else "yellow"
            table.add_row(
                r.called_at.strftime("%H:%M:%S"),
                r.tool_name,
                r.agent_id or "",
                f"[{status_style}]{r.status}[/{status_style}]",
                str(r.duration_ms),
                (r.error or "")[:40],
            )
        console.print(table)

    asyncio.run(_run())


@app.command()
def budget(
    studio: Annotated[str | None, typer.Option(help="Filter by studio_id")] = None,
    agent: Annotated[str | None, typer.Option(help="Filter by agent_id")] = None,
) -> None:
    """Show current-period budget buckets for a scope."""
    from studioos.budget import current_budget

    async def _run() -> None:
        async with session_scope() as session:
            views = await current_budget(
                session, agent_id=agent, studio_id=studio
            )
        if not views:
            console.print("[dim]No budget buckets for this scope[/dim]")
            return
        table = Table(title="Budgets")
        table.add_column("Scope", style="cyan")
        table.add_column("Period")
        table.add_column("Limit (¢)", justify="right")
        table.add_column("Spent (¢)", justify="right")
        table.add_column("Remaining (¢)", justify="right")
        table.add_column("Over")
        for v in views:
            style = "red" if v.over else "green"
            table.add_row(
                v.scope,
                v.period,
                str(v.limit_cents),
                str(v.spent_cents),
                f"[{style}]{v.remaining_cents}[/{style}]",
                "✗" if v.over else "✓",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("budget-set")
def budget_set(
    limit_cents: Annotated[int, typer.Argument(help="Daily limit in cents")],
    studio: Annotated[str | None, typer.Option(help="Studio scope")] = None,
    agent: Annotated[str | None, typer.Option(help="Agent scope")] = None,
    period: Annotated[str, typer.Option(help="day|month")] = "day",
) -> None:
    """Create or update a budget bucket for the current period."""
    from studioos.budget import ensure_budget

    async def _run() -> None:
        async with session_scope() as session:
            row = await ensure_budget(
                session,
                limit_cents=limit_cents,
                period=period,  # type: ignore[arg-type]
                agent_id=agent,
                studio_id=studio,
            )
        console.print(
            f"[green]Budget set:[/green] "
            f"{'agent=' + agent if agent else 'studio=' + (studio or '?')} "
            f"period={period} limit={limit_cents}¢ (id={row.id})"
        )

    asyncio.run(_run())


@app.command()
def approvals(
    state: Annotated[str, typer.Option(help="pending|approved|denied|expired|all")] = "pending",
    limit: Annotated[int, typer.Option()] = 50,
) -> None:
    """List approvals."""
    from sqlalchemy import desc, select

    from studioos.models import Approval

    async def _run() -> None:
        async with session_scope() as session:
            stmt = select(Approval).order_by(desc(Approval.created_at)).limit(limit)
            if state != "all":
                stmt = stmt.where(Approval.state == state)
            rows = (await session.execute(stmt)).scalars().all()
        table = Table(title=f"Approvals ({state})")
        table.add_column("ID", style="cyan")
        table.add_column("Run", style="dim")
        table.add_column("Agent", style="magenta")
        table.add_column("State", style="yellow")
        table.add_column("Reason")
        table.add_column("Created", style="dim")
        for r in rows:
            table.add_row(
                str(r.id)[:8],
                str(r.run_id)[:8],
                r.agent_id,
                r.state,
                r.reason[:40],
                r.created_at.strftime("%H:%M:%S"),
            )
        console.print(table)

    asyncio.run(_run())


@app.command()
def approve(
    approval_id: Annotated[str, typer.Argument(help="Approval id (full or prefix)")],
    note: Annotated[str | None, typer.Option(help="Decision note")] = None,
    by: Annotated[str, typer.Option(help="Who is approving")] = "cli",
) -> None:
    """Approve a pending approval."""
    _decide(approval_id, decision="approved", decided_by=by, note=note)


@app.command()
def deny(
    approval_id: Annotated[str, typer.Argument(help="Approval id (full or prefix)")],
    note: Annotated[str | None, typer.Option(help="Decision note")] = None,
    by: Annotated[str, typer.Option(help="Who is denying")] = "cli",
) -> None:
    """Deny a pending approval."""
    _decide(approval_id, decision="denied", decided_by=by, note=note)


def _decide(
    approval_id: str, *, decision: str, decided_by: str, note: str | None
) -> None:
    from sqlalchemy import select

    from studioos.approvals import decide_approval
    from studioos.models import Approval

    async def _run() -> None:
        async with session_scope() as session:
            # Allow prefix match
            rows = (
                (
                    await session.execute(
                        select(Approval).where(Approval.state == "pending")
                    )
                )
                .scalars()
                .all()
            )
            match = [r for r in rows if str(r.id).startswith(approval_id)]
            if not match:
                console.print(
                    f"[red]No pending approval matching {approval_id}[/red]"
                )
                return
            if len(match) > 1:
                console.print(
                    f"[red]Ambiguous prefix — {len(match)} matches[/red]"
                )
                return
            row = await decide_approval(
                session,
                approval_id=match[0].id,
                decision=decision,
                decided_by=decided_by,
                note=note,
            )
        console.print(
            f"[green]Approval {str(row.id)[:8]} {decision} by {decided_by}[/green]"
        )

    asyncio.run(_run())


@app.command("schedule")
def schedule_cmd() -> None:
    """List agents with a scheduled cadence + when they last ran."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from studioos.models import Agent
    from studioos.scheduler.parser import ScheduleError, parse_schedule

    async def _run() -> None:
        async with session_scope() as session:
            stmt = (
                select(Agent)
                .where(Agent.schedule_cron.is_not(None))
                .order_by(Agent.id)
            )
            rows = (await session.execute(stmt)).scalars().all()

        if not rows:
            console.print("[dim]No scheduled agents[/dim]")
            return

        table = Table(title=f"Scheduled agents ({len(rows)})")
        table.add_column("Agent", style="cyan")
        table.add_column("Studio", style="magenta")
        table.add_column("Schedule")
        table.add_column("Cadence")
        table.add_column("Last run", style="dim")
        table.add_column("Next due")
        now = datetime.now(UTC)
        for r in rows:
            try:
                schedule = parse_schedule(r.schedule_cron or "")
                cadence_str = schedule.display_cadence()
            except ScheduleError as exc:
                schedule = None
                cadence_str = f"[red]{exc}[/red]"
            last = (
                r.last_scheduled_at.strftime("%H:%M:%S")
                if r.last_scheduled_at
                else "-"
            )
            if r.last_scheduled_at is not None and schedule is not None:
                next_due = schedule.next_fire_after(r.last_scheduled_at)
                if next_due <= now:
                    next_str = "[green]now[/green]"
                else:
                    remaining = next_due - now
                    next_str = str(remaining).split(".")[0]
            else:
                next_str = "[green]now[/green]"
            table.add_row(
                r.id,
                r.studio_id,
                r.schedule_cron or "",
                cadence_str,
                last,
                next_str,
            )
        console.print(table)

    asyncio.run(_run())


@app.command()
def status() -> None:
    """One-shot overview: agents, runs, events, budget, approvals."""
    from datetime import UTC, datetime

    from rich.columns import Columns
    from rich.panel import Panel

    from studioos.status import build_snapshot

    async def _run() -> None:
        async with session_scope() as session:
            snap = await build_snapshot(session)

        # --- Studios + agents ---
        agents_table = Table(
            title=f"Agents ({len(snap.agents)})", expand=False
        )
        agents_table.add_column("Agent", style="cyan")
        agents_table.add_column("Studio", style="magenta")
        agents_table.add_column("Mode")
        agents_table.add_column("Schedule")
        agents_table.add_column("Next due")
        for a in snap.agents:
            mode_color = (
                "green" if a.mode == "normal"
                else "yellow" if a.mode == "degraded"
                else "red"
            )
            if a.schedule_cron is None:
                next_due = "[dim]—[/dim]"
            elif a.next_due_seconds is None:
                next_due = "[red]bad schedule[/red]"
            elif a.next_due_seconds <= 0:
                next_due = "[green]now[/green]"
            else:
                hrs, rem = divmod(a.next_due_seconds, 3600)
                mins, secs = divmod(rem, 60)
                next_due = (
                    f"{hrs:02d}:{mins:02d}:{secs:02d}"
                    if hrs
                    else f"{mins:02d}:{secs:02d}"
                )
            agents_table.add_row(
                a.id,
                a.studio_id,
                f"[{mode_color}]{a.mode}[/{mode_color}]",
                a.schedule_cron or "[dim]—[/dim]",
                next_due,
            )
        console.print(agents_table)

        # --- Run state histogram ---
        if snap.runs_by_state:
            rs_line = "  ".join(
                f"[bold]{k}[/bold]={v}"
                for k, v in sorted(snap.runs_by_state.items())
            )
            failures_line = (
                f"[red]failures_last_hour={snap.failures_last_hour}[/red]"
                if snap.failures_last_hour
                else f"[green]failures_last_hour=0[/green]"
            )
            console.print(
                Panel(
                    f"{rs_line}\n{failures_line}",
                    title="Runs",
                    expand=False,
                )
            )

        # --- Recent runs ---
        runs_table = Table(
            title=f"Recent runs ({len(snap.recent_runs)})", expand=False
        )
        runs_table.add_column("When", style="dim")
        runs_table.add_column("Agent", style="magenta")
        runs_table.add_column("State")
        runs_table.add_column("Trigger", style="dim")
        runs_table.add_column("Summary / Error")
        for r in snap.recent_runs:
            state_color = {
                "completed": "green",
                "running": "cyan",
                "pending": "yellow",
                "failed": "red",
                "budget_exceeded": "red",
                "awaiting_approval": "yellow",
                "dead": "red",
                "timed_out": "red",
            }.get(r.state, "white")
            body = r.error or r.summary or ""
            runs_table.add_row(
                r.created_at.strftime("%H:%M:%S"),
                r.agent_id,
                f"[{state_color}]{r.state}[/{state_color}]",
                r.trigger_type,
                body[:60],
            )
        console.print(runs_table)

        # --- Events last hour ---
        if snap.event_type_counts_last_hour:
            ev_table = Table(title="Events (last hour)", expand=False)
            ev_table.add_column("Type", style="cyan")
            ev_table.add_column("Count", justify="right")
            for t, c in sorted(
                snap.event_type_counts_last_hour.items(),
                key=lambda x: -x[1],
            ):
                ev_table.add_row(t, str(c))
            console.print(ev_table)

        # --- Tool usage last hour ---
        if snap.tool_call_counts_last_hour:
            tool_table = Table(title="Tools (last hour)", expand=False)
            tool_table.add_column("Tool", style="cyan")
            tool_table.add_column("Calls", justify="right")
            for t, c in sorted(
                snap.tool_call_counts_last_hour.items(),
                key=lambda x: -x[1],
            ):
                tool_table.add_row(t, str(c))
            tool_table.add_row(
                "[bold]total spend[/bold]",
                f"[bold]{snap.tool_cost_cents_last_hour}¢[/bold]",
            )
            console.print(tool_table)

        # --- Budgets ---
        if snap.budgets:
            b_table = Table(title="Budgets", expand=False)
            b_table.add_column("Scope", style="cyan")
            b_table.add_column("Period")
            b_table.add_column("Spent / Limit", justify="right")
            b_table.add_column("Remaining", justify="right")
            for b in snap.budgets:
                color = "red" if b["over"] else "green"
                b_table.add_row(
                    b["scope"],
                    b["period"],
                    f"{b['spent_cents']} / {b['limit_cents']}",
                    f"[{color}]{b['remaining_cents']}[/{color}]",
                )
            console.print(b_table)

        # --- Approvals ---
        ap_color = "yellow" if snap.pending_approvals > 0 else "dim"
        console.print(
            f"[{ap_color}]Pending approvals: {snap.pending_approvals}[/{ap_color}]"
        )
        console.print(
            f"[dim]As of {snap.as_of.strftime('%Y-%m-%d %H:%M:%S UTC')}[/dim]"
        )

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
