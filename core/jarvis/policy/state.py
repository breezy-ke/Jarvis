"""The kill switch and the autonomy gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.log import AuditLog
from jarvis.clock import Clock
from jarvis.db.models import SystemState

KILL_SWITCH_KEY = "kill_switch"


@dataclass(frozen=True)
class KillSwitchState:
    engaged: bool
    reason: str | None = None
    changed_at: datetime | None = None


async def get_kill_switch(session: AsyncSession) -> KillSwitchState:
    row = await session.scalar(select(SystemState).where(SystemState.key == KILL_SWITCH_KEY))
    if row is None:
        return KillSwitchState(engaged=False)
    return KillSwitchState(
        engaged=bool(row.value.get("engaged")),
        reason=row.value.get("reason"),
        changed_at=row.updated_at,
    )


async def set_kill_switch(
    session: AsyncSession,
    *,
    engaged: bool,
    reason: str | None,
    actor: str,
    audit: AuditLog,
    clock: Clock,
) -> KillSwitchState:
    row = await session.get(SystemState, KILL_SWITCH_KEY, with_for_update=True)
    now = clock.now()
    value = {"engaged": engaged, "reason": reason}
    if row is None:
        session.add(SystemState(key=KILL_SWITCH_KEY, value=value, updated_at=now))
    else:
        row.value = value
        row.updated_at = now
    await audit.append(
        session,
        actor=actor,
        event_type="system.kill_switch",
        summary="Kill switch engaged: all autonomy paused" if engaged else "Kill switch released",
        data={"engaged": engaged},
    )
    return KillSwitchState(engaged=engaged, reason=reason, changed_at=now)


@dataclass(frozen=True)
class GateStatus:
    open: bool
    reason: str


class AutonomyGate(Protocol):
    """Decides whether Jarvis may act on its own (L3) yet."""

    async def status(self, session: AsyncSession) -> GateStatus: ...


class StaticGate:
    """A gate with a fixed answer (used in tests)."""

    def __init__(self, *, open: bool, reason: str = "") -> None:
        self._status = GateStatus(open=open, reason=reason or ("open" if open else "closed"))

    async def status(self, session: AsyncSession) -> GateStatus:
        return self._status
