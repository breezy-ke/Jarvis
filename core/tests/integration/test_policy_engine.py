from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from jarvis.audit.log import AuditLog
from jarvis.clock import FrozenClock
from jarvis.db.models import ActionProposal, AuditEvent
from jarvis.db.session import SessionFactory, transaction
from jarvis.policy.engine import (
    ActionNotAvailable,
    ApprovalError,
    InvalidPayload,
    PolicyEngine,
    UnknownActionKind,
)
from jarvis.policy.registry import ActionRegistry
from jarvis.policy.state import StaticGate, set_kill_switch
from jarvis.policy.types import Channel, Status
from tests.policy_helpers import RecordingExecutor, SetContacts, build_engine, policies

pytestmark = pytest.mark.db


async def propose(
    factory: SessionFactory, engine: PolicyEngine, kind: str, **payload: object
) -> ActionProposal:
    async with transaction(factory) as session:
        return await engine.propose(
            session,
            kind=kind,
            payload={"text": "hello", **payload},
            rationale="test",
            created_by="agent:test",
        )


async def approve(
    factory: SessionFactory,
    engine: PolicyEngine,
    proposal: ActionProposal,
    *,
    channel: Channel = Channel.PWA,
    step_up: bool = False,
    approved_hash: str | None = None,
) -> ActionProposal:
    async with transaction(factory) as session:
        return await engine.approve(
            session,
            proposal.id,
            approved_hash=approved_hash or proposal.payload_hash,
            channel=channel,
            step_up_verified=step_up,
        )


async def reload(factory: SessionFactory, proposal_id: uuid.UUID) -> ActionProposal:
    async with factory() as session:
        row = await session.get(ActionProposal, proposal_id)
        assert row is not None
        return row


# --- Proposing -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("test.observe", Status.REFUSED),
        ("test.draft", Status.DRAFT_ONLY),
        ("test.ask", Status.PENDING),
        ("test.auto", Status.APPROVED),
    ],
)
async def test_autonomy_levels(
    session_factory: SessionFactory, clock: FrozenClock, kind: str, status: Status
) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, kind)
    assert proposal.status == status


async def test_l3_waits_when_the_gate_is_closed(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, _ = build_engine(clock=clock, gate_open=False)
    proposal = await propose(session_factory, engine, "test.auto")
    assert proposal.status == Status.PENDING
    assert "profile not signed off" in (proposal.status_reason or "")


async def test_l3_waits_when_the_kill_switch_is_on(
    session_factory: SessionFactory, clock: FrozenClock, audit: AuditLog
) -> None:
    engine, _ = build_engine(clock=clock, audit=audit)
    async with transaction(session_factory) as session:
        await set_kill_switch(
            session, engaged=True, reason="test", actor="owner", audit=audit, clock=clock
        )
    proposal = await propose(session_factory, engine, "test.auto")
    assert proposal.status == Status.PENDING
    assert "Kill switch" in (proposal.status_reason or "")


async def test_l3_with_a_warning_drops_to_approval(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.auto", text="KRA PIN A123456789Z")
    assert proposal.status == Status.PENDING
    assert "Needs your review" in (proposal.status_reason or "")


async def test_blocked_validation_refuses(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.auto", text="AKIAABCDEFGHIJKLMNOP")
    assert proposal.status == Status.REFUSED
    async with transaction(session_factory) as session:
        with pytest.raises(ApprovalError, match="can't be approved"):
            await engine.approve(
                session, proposal.id, approved_hash=proposal.payload_hash, channel=Channel.PWA
            )


async def test_unknown_and_unavailable_kinds(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, _ = build_engine(clock=clock)
    with pytest.raises(UnknownActionKind):
        await propose(session_factory, engine, "nope.never")
    empty = PolicyEngine(
        config=policies(),
        registry=ActionRegistry(),
        audit=AuditLog(clock),
        clock=clock,
        gate=StaticGate(open=True),
        contacts=SetContacts(),
    )
    with pytest.raises(ActionNotAvailable):
        await propose(session_factory, empty, "test.ask")


async def test_malformed_payload(session_factory: SessionFactory, clock: FrozenClock) -> None:
    engine, _ = build_engine(clock=clock)
    with pytest.raises(InvalidPayload):
        await propose(session_factory, engine, "test.ask", text="")


async def test_every_proposal_is_audited(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    async with session_factory() as session:
        events = list(await session.scalars(select(AuditEvent.event_type)))
        verify = await AuditLog(clock).verify(session)
    assert events == ["action.proposed"]
    assert verify.ok
    assert proposal.payload_hash in (await _audit_data(session_factory))[0]["payload_hash"]


async def _audit_data(factory: SessionFactory) -> list[dict[str, object]]:
    async with factory() as session:
        return [e.data for e in await session.scalars(select(AuditEvent).order_by(AuditEvent.id))]


# --- Approving -----------------------------------------------------------------


async def test_approval_is_bound_to_the_payload_hash(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    with pytest.raises(ApprovalError, match="changed since you saw it"):
        await approve(session_factory, engine, proposal, approved_hash="f" * 64)


async def test_high_risk_requires_a_fresh_passkey(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.risky")
    for channel in (Channel.PWA, Channel.TELEGRAM, Channel.VOICE):
        with pytest.raises(ApprovalError, match="only be approved via: pwa_passkey"):
            await approve(session_factory, engine, proposal, channel=channel)
    with pytest.raises(ApprovalError, match="Confirm with your passkey"):
        await approve(session_factory, engine, proposal, channel=Channel.PWA_PASSKEY)
    approved = await approve(
        session_factory, engine, proposal, channel=Channel.PWA_PASSKEY, step_up=True
    )
    assert approved.status == Status.APPROVED


async def test_tampered_payload_cannot_be_approved(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    async with transaction(session_factory) as session:
        await session.execute(
            update(ActionProposal)
            .where(ActionProposal.id == proposal.id)
            .values(payload_canonical=proposal.payload_canonical.replace("hello", "wire money"))
        )
    with pytest.raises(ApprovalError, match="Integrity check failed"):
        await approve(session_factory, engine, proposal)


async def test_cannot_approve_twice(session_factory: SessionFactory, clock: FrozenClock) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    await approve(session_factory, engine, proposal)
    with pytest.raises(ApprovalError, match="it is approved"):
        await approve(session_factory, engine, proposal)


async def test_reject(session_factory: SessionFactory, clock: FrozenClock) -> None:
    engine, executor = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    async with transaction(session_factory) as session:
        await engine.reject(session, proposal.id, channel=Channel.PWA, reason="not now")
    clock.advance(hours=1)
    await engine.execute_due(session_factory)
    assert executor.calls == []
    assert (await reload(session_factory, proposal.id)).status == Status.REJECTED


# --- Undo window and execution ---------------------------------------------------------


async def test_nothing_runs_before_the_undo_window_ends(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, executor = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    await approve(session_factory, engine, proposal)
    clock.advance(seconds=59)
    assert await engine.execute_due(session_factory) == []
    clock.advance(seconds=1)
    assert await engine.execute_due(session_factory) == [proposal.id]
    assert len(executor.calls) == 1
    done = await reload(session_factory, proposal.id)
    assert done.status == Status.EXECUTED
    assert done.result == {"echo": "hello"}


async def test_undo_inside_the_window(session_factory: SessionFactory, clock: FrozenClock) -> None:
    engine, executor = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    await approve(session_factory, engine, proposal)
    clock.advance(seconds=30)
    async with transaction(session_factory) as session:
        await engine.cancel(session, proposal.id, channel=Channel.PWA)
    clock.advance(minutes=5)
    await engine.execute_due(session_factory)
    assert executor.calls == []
    assert (await reload(session_factory, proposal.id)).status == Status.CANCELLED


async def test_undo_after_the_window_is_refused(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    await approve(session_factory, engine, proposal)
    clock.advance(seconds=61)
    async with transaction(session_factory) as session:
        with pytest.raises(ApprovalError, match="Too late to undo"):
            await engine.cancel(session, proposal.id, channel=Channel.PWA)


async def test_kill_switch_stops_execution(
    session_factory: SessionFactory, clock: FrozenClock, audit: AuditLog
) -> None:
    engine, executor = build_engine(clock=clock, audit=audit)
    proposal = await propose(session_factory, engine, "test.ask")
    await approve(session_factory, engine, proposal)
    async with transaction(session_factory) as session:
        await set_kill_switch(
            session, engaged=True, reason=None, actor="owner", audit=audit, clock=clock
        )
    clock.advance(minutes=5)
    assert await engine.execute_due(session_factory) == []
    assert executor.calls == []
    async with transaction(session_factory) as session:
        await set_kill_switch(
            session, engaged=False, reason=None, actor="owner", audit=audit, clock=clock
        )
    assert await engine.execute_due(session_factory) == [proposal.id]


async def test_executes_the_approved_bytes_not_the_json_column(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, executor = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    await approve(session_factory, engine, proposal)
    async with transaction(session_factory) as session:
        await session.execute(
            update(ActionProposal)
            .where(ActionProposal.id == proposal.id)
            .values(payload={"text": "something else entirely"})
        )
    clock.advance(minutes=2)
    await engine.execute_due(session_factory)
    assert executor.calls[0][1]["text"] == "hello"


async def test_tampering_after_approval_fails_closed(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, executor = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    await approve(session_factory, engine, proposal)
    async with transaction(session_factory) as session:
        await session.execute(
            update(ActionProposal)
            .where(ActionProposal.id == proposal.id)
            .values(payload_canonical=proposal.payload_canonical.replace("hello", "bye"))
        )
    clock.advance(minutes=2)
    await engine.execute_due(session_factory)
    assert executor.calls == []
    row = await reload(session_factory, proposal.id)
    assert row.status == Status.FAILED
    assert "Integrity" in (row.status_reason or "")


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("error", Status.FAILED),
        ("unknown", Status.UNKNOWN_OUTCOME),
        ("hang", Status.UNKNOWN_OUTCOME),
    ],
)
async def test_execution_failures(
    session_factory: SessionFactory, clock: FrozenClock, mode: str, status: Status
) -> None:
    engine, _ = build_engine(clock=clock, executor=RecordingExecutor(mode=mode), timeout=0.2)
    proposal = await propose(session_factory, engine, "test.ask")
    await approve(session_factory, engine, proposal)
    clock.advance(minutes=2)
    await engine.execute_due(session_factory)
    row = await reload(session_factory, proposal.id)
    assert row.status == status
    assert row.error


async def test_l3_auto_approved_runs_without_a_human(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, executor = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.auto")
    assert proposal.decided_via == "policy"
    await engine.execute_due(session_factory)
    assert len(executor.calls) == 1


async def test_auto_approval_is_rechecked_at_execution(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    open_engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, open_engine, "test.auto")
    closed_engine, executor = build_engine(clock=clock, gate_open=False)
    await closed_engine.execute_due(session_factory)
    assert executor.calls == []
    assert (await reload(session_factory, proposal.id)).status == Status.PENDING


async def test_daily_cap(session_factory: SessionFactory, clock: FrozenClock) -> None:
    engine, _ = build_engine(clock=clock)
    first = await propose(session_factory, engine, "test.capped")
    second = await propose(session_factory, engine, "test.capped")
    third = await propose(session_factory, engine, "test.capped")
    assert [first.status, second.status, third.status] == [
        Status.APPROVED,
        Status.APPROVED,
        Status.PENDING,
    ]
    with pytest.raises(ApprovalError, match="Daily limit"):
        await approve(session_factory, engine, third)
    clock.advance(hours=25)
    assert (await approve(session_factory, engine, third)).status == Status.APPROVED


async def test_quiet_hours_defer_execution(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    # 22:00 in Nairobi (UTC+3) is 19:00 UTC.
    clock.set(datetime(2026, 1, 5, 19, 0, tzinfo=UTC))
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.quiet")
    approved = await approve(session_factory, engine, proposal)
    assert approved.execute_after == datetime(2026, 1, 6, 4, 0, tzinfo=UTC)  # 07:00 Nairobi


async def test_crash_recovery_never_retries(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    engine, executor = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    await approve(session_factory, engine, proposal)
    async with transaction(session_factory) as session:
        await session.execute(
            update(ActionProposal)
            .where(ActionProposal.id == proposal.id)
            .values(status=Status.EXECUTING)
        )
    async with transaction(session_factory) as session:
        assert await engine.recover_interrupted(session) == 1
    clock.advance(hours=1)
    await engine.execute_due(session_factory)
    assert executor.calls == []
    assert (await reload(session_factory, proposal.id)).status == Status.UNKNOWN_OUTCOME


async def test_stale_proposals_expire(session_factory: SessionFactory, clock: FrozenClock) -> None:
    engine, _ = build_engine(clock=clock)
    proposal = await propose(session_factory, engine, "test.ask")
    clock.advance(days=8)
    async with transaction(session_factory) as session:
        assert await engine.expire_stale(session, max_age=timedelta(days=7)) == 1
    assert (await reload(session_factory, proposal.id)).status == Status.EXPIRED


# --- Validators wired through the engine --------------------------------------------------


async def test_mail_validators(session_factory: SessionFactory, clock: FrozenClock) -> None:
    engine, _ = build_engine(clock=clock, known_contacts={"client@acme.co.ke"})
    known = await propose(session_factory, engine, "test.mail", to=["client@acme.co.ke"])
    assert known.status == Status.PENDING
    assert known.status_reason is None
    stranger = await propose(
        session_factory, engine, "test.mail", to=["Someone <someone@else.com>"]
    )
    assert "New recipient" in (stranger.status_reason or "")
    missing = await propose(
        session_factory,
        engine,
        "test.mail",
        to=["client@acme.co.ke"],
        text="Please see the attached proposal.",
    )
    assert "mentions an attachment" in (missing.status_reason or "")
    no_recipients = await propose(session_factory, engine, "test.mail")
    assert no_recipients.status == Status.REFUSED
    evil_link = await propose(
        session_factory,
        engine,
        "test.mail",
        to=["client@acme.co.ke"],
        text="click javascript:alert(1)",
    )
    assert evil_link.status == Status.REFUSED
