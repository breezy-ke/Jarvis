from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select, update

from jarvis.audit.log import AuditLog
from jarvis.clock import FrozenClock
from jarvis.db.models import AuditEvent
from jarvis.db.session import SessionFactory, transaction

pytestmark = pytest.mark.db


async def _append_many(
    factory: SessionFactory, audit: AuditLog, clock: FrozenClock, n: int
) -> None:
    for i in range(n):
        clock.advance(seconds=1)
        async with transaction(factory) as session:
            await audit.append(
                session, actor="test", event_type="test.event", summary=f"event {i}", data={"i": i}
            )


async def test_chain_verifies(
    session_factory: SessionFactory, audit: AuditLog, clock: FrozenClock
) -> None:
    await _append_many(session_factory, audit, clock, 5)
    async with session_factory() as session:
        result = await audit.verify(session)
    assert result.ok
    assert result.checked == 5


async def test_first_event_links_to_genesis(
    session_factory: SessionFactory, audit: AuditLog, clock: FrozenClock
) -> None:
    await _append_many(session_factory, audit, clock, 1)
    async with session_factory() as session:
        first = await session.scalar(select(AuditEvent))
    assert first is not None
    assert first.prev_hash == "0" * 64


@pytest.mark.parametrize(
    "tamper",
    [
        update(AuditEvent).where(AuditEvent.id == 3).values(summary="nothing to see here"),
        update(AuditEvent).where(AuditEvent.id == 3).values(data={"i": 99}),
        update(AuditEvent).where(AuditEvent.id == 3).values(canonical=AuditEvent.canonical + " "),
        delete(AuditEvent).where(AuditEvent.id == 3),
    ],
    ids=["edit-summary", "edit-data", "edit-body", "delete-row"],
)
async def test_tampering_is_detected(
    session_factory: SessionFactory, audit: AuditLog, clock: FrozenClock, tamper: object
) -> None:
    await _append_many(session_factory, audit, clock, 5)
    async with transaction(session_factory) as session:
        await session.execute(tamper)  # type: ignore[arg-type]
    async with session_factory() as session:
        result = await audit.verify(session)
    assert not result.ok
    assert result.first_bad_id in {3, 4}


async def test_concurrent_writers_never_fork_the_chain(
    session_factory: SessionFactory, audit: AuditLog
) -> None:
    async def writer(n: int) -> None:
        async with transaction(session_factory) as session:
            await audit.append(session, actor="w", event_type="t", summary=f"w{n}")

    await asyncio.gather(*(writer(n) for n in range(20)))
    async with session_factory() as session:
        result = await audit.verify(session)
        prevs = list(await session.scalars(select(AuditEvent.prev_hash)))
    assert result.ok
    assert result.checked == 20
    assert len(prevs) == len(set(prevs))  # no two events share a parent
