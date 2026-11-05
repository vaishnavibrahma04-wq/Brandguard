"""Simulated clock so SLAs, approval timeouts and monitoring windows are deterministic."""
from __future__ import annotations
from datetime import datetime, timedelta


class SimClock:
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 10, 5, 9, 0, 0)

    def now(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt

    def advance(self, minutes: float = 0, hours: float = 0) -> datetime:
        self._now += timedelta(minutes=minutes, hours=hours)
        return self._now

    def iso(self) -> str:
        return self._now.isoformat(timespec="seconds")
