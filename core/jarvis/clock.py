"""A swappable clock so time-dependent rules (undo windows, quotas) are testable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test clock: time only moves when you call `advance`."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value
