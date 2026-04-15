"""Agent scheduler — cadence-based triggers for agents with schedule_cron.

Current schedule syntax is deliberately minimal: `@every <duration>`
where duration is a Go-style compact string, e.g. `30s`, `5m`, `1h`,
`2h30m`, `1h15m30s`. Anything richer (weekday filters, exact times)
can land later as a `@cron` variant backed by croniter without
changing the caller API.
"""
from __future__ import annotations

from .loop import scheduler_loop, tick_once
from .parser import ScheduleError, parse_schedule

__all__ = [
    "ScheduleError",
    "parse_schedule",
    "scheduler_loop",
    "tick_once",
]
