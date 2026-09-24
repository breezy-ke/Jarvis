"""Property-based test: no sequence of events can make the engine unsafe.

Hypothesis generates random sequences of operations: proposals of every
autonomy level, approvals through every channel (right or wrong hash,
with or without a passkey), rejections, undos, clock jumps, kill-switch and
gate toggles, database tampering, and executor runs. After every run of the
executor, the invariants below must hold.

  1. Nothing executes more than once.
  2. Nothing executes while the kill switch is on.
  3. Nothing executes before its undo window ends.
  4. Everything that executes was approved with the hash of exactly its bytes,
     through a channel allowed for its risk, or auto-approved at L3.
  5. A proposal tampered with before execution never executes.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from jarvis.audit.log import AuditLog
from jarvis.clock import FrozenClock
from jarvis.db.base import Base
from jarvis.db.models import ActionProposal
from jarvis.db.session import SessionFactory, create_session_factory, transaction
from jarvis.policy.engine import PolicyEngine, PolicyError
from jarvis.policy.state import GateStatus, set_kill_switch
from jarvis.policy.types import POLICY_CHANNEL, Channel
from jarvis.security.hashing import sha256_hex
from tests.conftest import TEST_DATABASE_URL
from tests.policy_helpers import build_engine

pytestmark = [pytest.mark.db, pytest.mark.slow]

KINDS = ["test.observe", "test.draft", "test.ask", "test.auto", "test.risky", "test.capped"]
TEXTS = ["hello", "KRA A123456789Z", "key AKIAABCDEFGHIJKLMNOP"]

_mostly_true = st.sampled_from([True, True, True, False])
_recent = st.one_of(st.integers(-2, -1), st.integers(0, 30))

_propose = st.tuples(
    st.just("propose"),
    st.sampled_from([*KINDS, "test.ask", "test.auto", "test.risky"]),
    st.sampled_from([*TEXTS, "hello", "hello"]),
)
_approve = st.tuples(
    st.just("approve"),
    _recent,
    st.sampled_from([*list(Channel), Channel.PWA, Channel.PWA_PASSKEY]),
    _mostly_true,  # passkey step-up verified
    _mostly_true,  # correct hash
)
_advance = st.tuples(st.just("advance"), st.sampled_from([0, 10, 59, 61, 61, 3600, 90_000]))
_execute = st.tuples(st.just("execute"))

# Repeating a strategy in one_of makes it more likely, so most examples
# actually reach execution.
op = st.one_of(
    _propose,
    _propose,
    _approve,
    _approve,
    _approve,
    st.tuples(st.just("reject"), _recent),
    st.tuples(st.just("cancel"), _recent),
    _advance,
    _advance,
    # Mostly "off"/"open": a kill switch left on would make most runs trivially
    # safe and hide bugs in the execution path.
    st.tuples(st.just("kill"), st.sampled_from([False, False, False, True])),
    st.tuples(st.just("gate"), _mostly_true),
    st.tuples(st.just("tamper"), _recent),
    _execute,
    _execute,
)


class MutableGate:
    def __init__(self) -> None:
        self.open = True

    async def status(self, session: object) -> GateStatus:
        return GateStatus(open=self.open, reason="toggled by test")


@dataclass
class World:
    ids: list[uuid.UUID] = field(default_factory=list)
    tampered: set[uuid.UUID] = field(default_factory=set)
    executed: set[uuid.UUID] = field(default_factory=set)
    kill: bool = False


async def _reset(engine: AsyncEngine) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


async def _check_new_executions(
    factory: SessionFactory, engine: PolicyEngine, world: World, calls: list, clock: FrozenClock
) -> None:
    seen: dict[str, int] = {}
    for proposal_id, _payload in calls:
        seen[proposal_id] = seen.get(proposal_id, 0) + 1
    assert all(count == 1 for count in seen.values()), "an action executed twice"
    async with factory() as session:
        for raw_id in seen:
            pid = uuid.UUID(raw_id)
            if pid in world.executed:
                continue
            world.executed.add(pid)
            row = await session.get(ActionProposal, pid)
            assert row is not None
            assert not world.kill, "executed while the kill switch was on"
            assert pid not in world.tampered, "a tampered action executed"
            assert row.execute_after is not None
            assert row.execute_after <= clock.now(), "executed before the undo window ended"
            digest = sha256_hex(row.payload_canonical)
            assert row.approved_hash == digest == row.payload_hash
            policy = engine.config.action_kinds[row.kind]
            if row.decided_via == POLICY_CHANNEL:
                assert policy.autonomy.value == "L3"
            else:
                allowed = {c.value for c in engine.config.approval_channels[policy.risk]}
                assert row.decided_via in allowed


async def _run(ops: list[tuple]) -> None:
    db = create_async_engine(TEST_DATABASE_URL, pool_size=2)
    try:
        await _reset(db)
        factory = create_session_factory(db)
        clock = FrozenClock()
        audit = AuditLog(clock)
        gate = MutableGate()
        engine, executor = build_engine(clock=clock, audit=audit)
        engine._gate = gate  # swap in the toggleable gate
        world = World()

        def pick(index: int) -> uuid.UUID | None:
            return world.ids[index % len(world.ids)] if world.ids else None

        for step in ops:
            name = step[0]
            try:
                if name == "propose":
                    async with transaction(factory) as session:
                        p = await engine.propose(
                            session,
                            kind=step[1],
                            payload={"text": step[2]},
                            rationale="prop",
                            created_by="agent:prop",
                        )
                    world.ids.append(p.id)
                elif name == "approve" and (pid := pick(step[1])):
                    async with factory() as session:
                        row = await session.get(ActionProposal, pid)
                        assert row is not None
                        digest = row.payload_hash if step[4] else "0" * 64
                    async with transaction(factory) as session:
                        await engine.approve(
                            session,
                            pid,
                            approved_hash=digest,
                            channel=step[2],
                            step_up_verified=step[3],
                        )
                elif name == "reject" and (pid := pick(step[1])):
                    async with transaction(factory) as session:
                        await engine.reject(session, pid, channel=Channel.PWA)
                elif name == "cancel" and (pid := pick(step[1])):
                    async with transaction(factory) as session:
                        await engine.cancel(session, pid, channel=Channel.PWA)
                elif name == "advance":
                    clock.advance(seconds=step[1])
                elif name == "kill":
                    world.kill = step[1]
                    async with transaction(factory) as session:
                        await set_kill_switch(
                            session,
                            engaged=step[1],
                            reason=None,
                            actor="t",
                            audit=audit,
                            clock=clock,
                        )
                elif name == "gate":
                    gate.open = step[1]
                elif name == "tamper" and (pid := pick(step[1])) and pid not in world.executed:
                    world.tampered.add(pid)
                    async with transaction(factory) as session:
                        await session.execute(
                            update(ActionProposal)
                            .where(ActionProposal.id == pid)
                            .values(payload_canonical=ActionProposal.payload_canonical + " ")
                        )
                elif name == "execute":
                    await engine.execute_due(factory)
                    await _check_new_executions(factory, engine, world, executor.calls, clock)
            except PolicyError:
                pass  # refusals are expected; the invariants are what matter

        await engine.execute_due(factory)
        await _check_new_executions(factory, engine, world, executor.calls, clock)
        event(f"executed {min(len(world.executed), 3)}+ actions")
        async with factory() as session:
            assert (await audit.verify(session)).ok
    finally:
        await db.dispose()


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(ops=st.lists(op, min_size=10, max_size=40))
def test_policy_engine_invariants(engine: AsyncEngine, ops: list[tuple]) -> None:
    # `engine` guarantees migrations ran; each example uses its own event loop.
    asyncio.run(_run(ops))
