"""Tiny schedule parser — `@every <duration>`."""
from __future__ import annotations

import re
from datetime import timedelta


class ScheduleError(ValueError):
    """Raised when a schedule string can't be parsed."""


_DURATION_RE = re.compile(r"(\d+)\s*(h|m|s)")


def _parse_duration(spec: str) -> timedelta:
    spec = spec.strip().lower()
    if not spec:
        raise ScheduleError("empty duration")
    matches = list(_DURATION_RE.finditer(spec))
    if not matches:
        raise ScheduleError(f"cannot parse duration {spec!r}")
    # Reject trailing/leading junk that didn't match.
    covered = sum(m.end() - m.start() for m in matches)
    stripped = re.sub(r"\s+", "", spec)
    if covered != len(stripped):
        raise ScheduleError(f"unexpected chars in duration {spec!r}")
    total = timedelta()
    for match in matches:
        value = int(match.group(1))
        unit = match.group(2)
        if unit == "h":
            total += timedelta(hours=value)
        elif unit == "m":
            total += timedelta(minutes=value)
        elif unit == "s":
            total += timedelta(seconds=value)
    if total <= timedelta(0):
        raise ScheduleError(f"duration must be positive, got {spec!r}")
    return total


def parse_schedule(spec: str) -> timedelta:
    """Parse a schedule string and return the cadence as a timedelta.

    Supported forms (single cadence only for now):
        @every 30s
        @every 15m
        @every 2h30m
    """
    if not spec:
        raise ScheduleError("empty schedule")
    spec = spec.strip()
    if spec.startswith("@every"):
        return _parse_duration(spec[len("@every"):])
    raise ScheduleError(f"unsupported schedule {spec!r} (expected @every ...)")
