"""Schedule parser — supports `@every <duration>` and standard cron."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


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


@dataclass
class Schedule:
    """A parsed schedule. Two flavors:

      kind == "every"  — fixed cadence (timedelta in `every`)
      kind == "cron"   — cron expression (raw string in `cron`)
    """

    kind: str  # "every" | "cron"
    every: timedelta | None = None
    cron: str | None = None

    def next_fire_after(self, when: datetime) -> datetime:
        """Compute the next firing time strictly after `when`."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if self.kind == "every":
            assert self.every is not None
            return when + self.every
        if self.kind == "cron":
            from croniter import croniter

            it = croniter(self.cron, when)
            return it.get_next(datetime)
        raise ScheduleError(f"unknown schedule kind {self.kind}")

    def display_cadence(self) -> str:
        if self.kind == "every":
            return str(self.every)
        return f"cron({self.cron})"

    # Back-compat shim: callers that still treat the return as timedelta
    # work for the @every case. Cron callers must use next_fire_after().
    def __add__(self, other: datetime) -> datetime:
        if isinstance(other, datetime):
            return self.next_fire_after(other)
        raise TypeError("Schedule + non-datetime")

    def __radd__(self, other: datetime) -> datetime:
        return self.__add__(other)


def parse_schedule(spec: str) -> Schedule:
    """Parse a schedule string.

    Supported forms:
        @every 30s
        @every 15m
        @every 2h30m
        @cron <crontab-style 5-field expression>     (M25)
        <crontab-style 5-field expression>           (M25, bare form)

    Returns a Schedule object. For backward compat with M7 callers
    that did `last + cadence`, `Schedule.__add__` makes the @every
    case still behave like a timedelta.
    """
    if not spec:
        raise ScheduleError("empty schedule")
    spec = spec.strip()
    if spec.startswith("@every"):
        delta = _parse_duration(spec[len("@every"):])
        return Schedule(kind="every", every=delta)
    if spec.startswith("@cron"):
        rest = spec[len("@cron"):].strip()
        return _parse_cron(rest)
    # Bare crontab form, e.g. "0 9 * * 1"
    if re.match(r"^[\d*,/\-]+(\s+[\d*,/\-]+){4,5}$", spec):
        return _parse_cron(spec)
    raise ScheduleError(
        f"unsupported schedule {spec!r} (expected @every ... or cron expr)"
    )


def _parse_cron(spec: str) -> Schedule:
    spec = spec.strip()
    if not spec:
        raise ScheduleError("empty cron expression")
    try:
        from croniter import croniter
    except ImportError as exc:
        raise ScheduleError(f"croniter unavailable: {exc}") from exc
    if not croniter.is_valid(spec):
        raise ScheduleError(f"invalid cron expression {spec!r}")
    return Schedule(kind="cron", cron=spec)
