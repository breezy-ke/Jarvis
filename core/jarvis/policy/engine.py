"""The policy engine: the only path from "an agent wants to" to "it happened".

Lifecycle of a proposal:

    propose ─► refused      (L0, or a validator blocked it)
            ├► draft_only   (L1)
            ├► pending ───► approved ─► executing ─► executed | failed
            │     │            │             └─► unknown_outcome (crashed mid-run)
            │     ├► rejected  └► cancelled (undone inside the undo window)
            │     └► expired
            └► approved     (L3 auto-approval: validators clean, gate open,
                             kill switch off, under the daily cap)

Guarantees, each covered by tests:
  * Only `approved` proposals execute, and only after `execute_after`.
  * An approval is bound to a SHA-256 of the canonical payload. What
    executes is re-parsed from exactly those bytes.
  * Approval channels follow the risk level; high risk needs a passkey.
  * The kill switch stops all execution immediately.
  * Execution is at-most-once. A proposal is marked `executing` (and
    committed) before its side effect runs, so a crash leaves
    `unknown_outcome` for the owner to review instead of a silent retry.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.log import AuditLog
from jarvis.clock import Clock
from jarvis.db.models import ActionProposal
from jarvis.db.session import SessionFactory, transaction
from jarvis.policy.config import ActionKindPolicy, PoliciesConfig
from jarvis.policy.registry import ActionRegistry, ExecutionContext, OutcomeUnknownError
from jarvis.policy.state import AutonomyGate, get_kill_switch
from jarvis.policy.types import (
    POLICY_CHANNEL,
    Autonomy,
    Channel,
    Status,
    ValidationResult,
)
from jarvis.policy.validators import VALIDATORS, KnownContacts, ValidationContext, ValidatorFn
from jarvis.security.hashing import payload_digest, sha256_hex


class PolicyError(Exception):
    """A user-facing refusal. The message is safe to show."""


class UnknownActionKind(PolicyError):
    pass


class ActionNotAvailable(PolicyError):
    pass


class InvalidPayload(PolicyError):
    pass


class ApprovalError(PolicyError):
    pass


_CAP_STATUSES = (
    Status.APPROVED,
    Status.EXECUTING,
    Status.EXECUTED,
    Status.UNKNOWN_OUTCOME,
)


class PolicyEngine:
    def __init__(
        self,
        *,
        config: PoliciesConfig,
        registry: ActionRegistry,
        audit: AuditLog,
        clock: Clock,
        gate: AutonomyGate,
        contacts: KnownContacts,
        validators: dict[str, ValidatorFn] | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self._audit = audit
        self._clock = clock
        self._gate = gate
        self._contacts = contacts
        self._validators = validators or VALIDATORS
        missing = {
            name
            for policy in config.action_kinds.values()
            for name in policy.validators
            if name not in self._validators
        }
        if missing:
            raise ValueError(f"policies.yaml names unknown validators: {sorted(missing)}")

    # --- Proposing --------------------------------------------------------------

    async def propose(
        self,
        session: AsyncSession,
        *,
        kind: str,
        payload: dict[str, Any],
        rationale: str,
        created_by: str,
        evidence: list[dict[str, Any]] | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> ActionProposal:
        policy = self.config.action_kinds.get(kind)
        if policy is None:
            raise UnknownActionKind(f"'{kind}' is not an action Jarvis knows about.")
        spec = self.registry.get(kind)
        if spec is None:
            raise ActionNotAvailable(f"'{kind}' isn't available yet.")
        try:
            model = spec.payload_model.model_validate(payload)
        except ValidationError as exc:
            raise InvalidPayload(f"The {kind} request is incomplete or malformed: {exc}") from exc
        clean = model.model_dump(mode="json")
        canonical, digest = payload_digest(kind, clean)
        now = self._clock.now()

        results = await self._run_validators(session, kind, clean, policy, now)
        blocked = [r for r in results if r.outcome == "block"]
        warned = [r for r in results if r.outcome == "warn"]

        proposal = ActionProposal(
            id=uuid.uuid4(),
            kind=kind,
            payload=clean,
            payload_canonical=canonical,
            payload_hash=digest,
            risk=policy.risk.value,
            autonomy=policy.autonomy.value,
            summary=spec.summarize(model),
            rationale=rationale,
            evidence=evidence or [],
            validation=[r.to_dict() for r in results],
            created_by=created_by,
            conversation_id=conversation_id,
            created_at=now,
        )

        if policy.autonomy == Autonomy.L0:
            proposal.status, proposal.status_reason = Status.REFUSED, "Policy: observe only"
        elif blocked:
            proposal.status = Status.REFUSED
            proposal.status_reason = "; ".join(r.message for r in blocked)
        elif policy.autonomy == Autonomy.L1:
            proposal.status, proposal.status_reason = Status.DRAFT_ONLY, "Policy: draft only"
        elif policy.autonomy == Autonomy.L3 and not warned:
            reason = await self._auto_approval_blocker(session, kind, policy, now)
            if reason is None:
                proposal.status = Status.APPROVED
                proposal.decided_at = now
                proposal.decided_via = POLICY_CHANNEL
                proposal.approved_hash = digest
                proposal.execute_after = self._execute_after(kind, policy, now)
            else:
                proposal.status, proposal.status_reason = Status.PENDING, reason
        else:
            proposal.status = Status.PENDING
            if warned:
                proposal.status_reason = "Needs your review: " + "; ".join(
                    r.message for r in warned
                )

        session.add(proposal)
        await session.flush()
        await self._audit.append(
            session,
            actor=created_by,
            event_type="action.proposed",
            subject_type="action",
            subject_id=str(proposal.id),
            summary=f"Proposed {kind}: {proposal.status}",
            data={
                "kind": kind,
                "status": proposal.status,
                "risk": proposal.risk,
                "autonomy": proposal.autonomy,
                "payload_hash": digest,
                "checks": {r.validator: r.outcome for r in results},
            },
        )
        return proposal

    async def _run_validators(
        self,
        session: AsyncSession,
        kind: str,
        payload: dict[str, Any],
        policy: ActionKindPolicy,
        now: datetime,
    ) -> list[ValidationResult]:
        ctx = ValidationContext(
            kind=kind,
            payload=payload,
            policy=policy,
            session=session,
            now=now,
            contacts=self._contacts,
        )
        results: list[ValidationResult] = []
        for name in policy.validators:
            try:
                results.append(await self._validators[name](ctx))
            except Exception as exc:  # a crashing check must never let an action through
                results.append(ValidationResult(name, "block", f"Check failed to run: {exc}"))
        return results

    async def _auto_approval_blocker(
        self, session: AsyncSession, kind: str, policy: ActionKindPolicy, now: datetime
    ) -> str | None:
        if (await get_kill_switch(session)).engaged:
            return "Kill switch is on: waiting for your approval"
        gate = await self._gate.status(session)
        if not gate.open:
            return f"Autonomy paused: {gate.reason}"
        if await self._cap_reached(session, kind, policy, now):
            return f"Daily limit of {policy.max_per_day} reached: waiting for your approval"
        return None

    # --- Deciding -----------------------------------------------------------------

    async def _load_for_update(
        self, session: AsyncSession, proposal_id: uuid.UUID
    ) -> ActionProposal:
        proposal = await session.get(ActionProposal, proposal_id, with_for_update=True)
        if proposal is None:
            raise ApprovalError("That action no longer exists.")
        return proposal

    async def approve(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        *,
        approved_hash: str,
        channel: Channel,
        step_up_verified: bool = False,
        actor: str = "owner",
    ) -> ActionProposal:
        proposal = await self._load_for_update(session, proposal_id)
        if proposal.status != Status.PENDING:
            raise ApprovalError(f"This action can't be approved: it is {proposal.status}.")
        if sha256_hex(proposal.payload_canonical) != proposal.payload_hash:
            await self._audit.append(
                session,
                actor="system",
                event_type="action.integrity_failed",
                subject_type="action",
                subject_id=str(proposal.id),
                summary="Stored payload does not match its hash: approval refused",
            )
            raise ApprovalError(
                "Integrity check failed: this action was altered after it was proposed."
            )
        if approved_hash != proposal.payload_hash:
            raise ApprovalError("This action changed since you saw it. Review it again.")
        policy = self.config.action_kinds.get(proposal.kind)
        if policy is None:
            raise ApprovalError(f"'{proposal.kind}' is no longer allowed by your policies.")
        allowed = self.config.approval_channels[policy.risk]
        if channel not in allowed:
            names = ", ".join(c.value for c in allowed)
            raise ApprovalError(
                f"{policy.risk.value}-risk actions can only be approved via: {names}."
            )
        if channel == Channel.PWA_PASSKEY and not step_up_verified:
            raise ApprovalError("Confirm with your passkey to approve this.")
        now = self._clock.now()
        if await self._cap_reached(session, proposal.kind, policy, now):
            raise ApprovalError(f"Daily limit of {policy.max_per_day} reached. Try again tomorrow.")

        proposal.status = Status.APPROVED
        proposal.status_reason = None
        proposal.decided_at = now
        proposal.decided_via = channel.value
        proposal.approved_hash = approved_hash
        execute_after = self._execute_after(proposal.kind, policy, now)
        proposal.execute_after = execute_after
        await self._audit.append(
            session,
            actor=actor,
            event_type="action.approved",
            subject_type="action",
            subject_id=str(proposal.id),
            summary=f"Approved {proposal.kind} via {channel.value}",
            data={
                "kind": proposal.kind,
                "channel": channel.value,
                "payload_hash": approved_hash,
                "execute_after": execute_after.isoformat(),
            },
        )
        return proposal

    async def reject(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        *,
        channel: Channel,
        reason: str | None = None,
        actor: str = "owner",
    ) -> ActionProposal:
        proposal = await self._load_for_update(session, proposal_id)
        if proposal.status not in (Status.PENDING, Status.DRAFT_ONLY):
            raise ApprovalError(f"This action can't be rejected: it is {proposal.status}.")
        proposal.status = Status.REJECTED
        proposal.status_reason = reason or "Rejected by you"
        proposal.decided_at = self._clock.now()
        proposal.decided_via = channel.value
        await self._audit.append(
            session,
            actor=actor,
            event_type="action.rejected",
            subject_type="action",
            subject_id=str(proposal.id),
            summary=f"Rejected {proposal.kind}",
            data={"kind": proposal.kind, "channel": channel.value},
        )
        return proposal

    async def cancel(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        *,
        channel: Channel,
        actor: str = "owner",
    ) -> ActionProposal:
        """Undo an approval before it executes."""
        proposal = await self._load_for_update(session, proposal_id)
        now = self._clock.now()
        if proposal.status != Status.APPROVED:
            raise ApprovalError(f"Too late to undo: this action is {proposal.status}.")
        if proposal.execute_after is not None and now >= proposal.execute_after:
            raise ApprovalError("Too late to undo: it is already going out.")
        proposal.status = Status.CANCELLED
        proposal.status_reason = "Undone by you"
        await self._audit.append(
            session,
            actor=actor,
            event_type="action.cancelled",
            subject_type="action",
            subject_id=str(proposal.id),
            summary=f"Undid {proposal.kind} before it ran",
            data={"kind": proposal.kind, "channel": channel.value},
        )
        return proposal

    async def withdraw(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        *,
        reason: str,
        actor: str = "system",
    ) -> bool:
        """Take back a proposal nobody has decided on yet (replaced, or no longer needed).

        Returns False if it was already decided: an approval is never undone here.
        """
        proposal = await session.get(ActionProposal, proposal_id, with_for_update=True)
        if proposal is None or proposal.status not in (Status.PENDING, Status.DRAFT_ONLY):
            return False
        proposal.status = Status.CANCELLED
        proposal.status_reason = reason
        await self._audit.append(
            session,
            actor=actor,
            event_type="action.withdrawn",
            subject_type="action",
            subject_id=str(proposal.id),
            summary=f"Withdrew {proposal.kind}: {reason}"[:300],
            data={"kind": proposal.kind},
        )
        return True

    async def confirm_outcome(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        *,
        result: dict[str, Any],
        evidence: str,
    ) -> bool:
        """An action whose outcome was unknown turned out to have happened.

        Only `unknown_outcome` moves to `executed` here, with the evidence audited.
        """
        proposal = await session.get(ActionProposal, proposal_id, with_for_update=True)
        if proposal is None or proposal.status != Status.UNKNOWN_OUTCOME:
            return False
        proposal.status = Status.EXECUTED
        proposal.status_reason = f"Confirmed: {evidence}"
        proposal.executed_at = self._clock.now()
        proposal.result = result
        await self._audit.append(
            session,
            actor="system",
            event_type="action.confirmed",
            subject_type="action",
            subject_id=str(proposal.id),
            summary=f"Confirmed {proposal.kind} happened: {evidence}"[:300],
            data={"kind": proposal.kind},
        )
        return True

    # --- Executing ----------------------------------------------------------------

    async def execute_due(
        self, session_factory: SessionFactory, *, limit: int = 20
    ) -> list[uuid.UUID]:
        """Run every approved action whose undo window has passed."""
        claimed: list[tuple[uuid.UUID, str, str]] = []
        async with transaction(session_factory) as session:
            if (await get_kill_switch(session)).engaged:
                return []
            now = self._clock.now()
            rows = await session.scalars(
                select(ActionProposal)
                .where(ActionProposal.status == Status.APPROVED)
                .where(ActionProposal.execute_after <= now)
                .order_by(ActionProposal.execute_after)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            for proposal in rows:
                problem = await self._pre_execution_problem(session, proposal)
                if problem is not None:
                    status, reason = problem
                    proposal.status, proposal.status_reason = status, reason
                    await self._audit.append(
                        session,
                        actor="system",
                        event_type="action.blocked",
                        subject_type="action",
                        subject_id=str(proposal.id),
                        summary=f"Did not run {proposal.kind}: {reason}",
                        data={"kind": proposal.kind, "status": status},
                    )
                    continue
                proposal.status = Status.EXECUTING
                await self._audit.append(
                    session,
                    actor="system",
                    event_type="action.executing",
                    subject_type="action",
                    subject_id=str(proposal.id),
                    summary=f"Running {proposal.kind}",
                    data={"kind": proposal.kind, "payload_hash": proposal.payload_hash},
                )
                claimed.append((proposal.id, proposal.kind, proposal.payload_canonical))

        for proposal_id, kind, canonical in claimed:
            await self._run_one(session_factory, proposal_id, kind, canonical)
        return [c[0] for c in claimed]

    async def _pre_execution_problem(
        self, session: AsyncSession, proposal: ActionProposal
    ) -> tuple[Status, str] | None:
        digest = sha256_hex(proposal.payload_canonical)
        if digest != proposal.payload_hash or proposal.approved_hash != digest:
            return Status.FAILED, "Integrity check failed: payload does not match the approval"
        if self.registry.get(proposal.kind) is None:
            return Status.FAILED, f"'{proposal.kind}' is no longer available"
        policy = self.config.action_kinds.get(proposal.kind)
        if policy is None:
            return Status.FAILED, f"'{proposal.kind}' is no longer allowed by your policies"
        if proposal.decided_via == POLICY_CHANNEL:
            # Auto-approved: re-check that autonomy is still allowed right now.
            gate = await self._gate.status(session)
            if not gate.open or policy.autonomy != Autonomy.L3:
                return Status.PENDING, "Autonomy paused before it ran: waiting for your approval"
        return None

    async def _run_one(
        self, session_factory: SessionFactory, proposal_id: uuid.UUID, kind: str, canonical: str
    ) -> None:
        spec = self.registry.get(kind)
        assert spec is not None
        ctx = ExecutionContext(
            proposal_id=proposal_id, kind=kind, session_factory=session_factory, clock=self._clock
        )
        result: dict[str, Any] | None = None
        error: str | None = None
        outcome_unknown = False
        try:
            approved_payload = json.loads(canonical)["payload"]
            model = spec.payload_model.model_validate(approved_payload)
            result = await asyncio.wait_for(spec.executor(ctx, model), timeout=spec.timeout_seconds)
        except TimeoutError:
            outcome_unknown = True
            error = f"Timed out after {spec.timeout_seconds:.0f}s; it may or may not have happened"
        except OutcomeUnknownError as exc:
            outcome_unknown = True
            error = f"Outcome unknown: {exc}"[:2000]
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:2000]

        async with transaction(session_factory) as session:
            proposal = await session.get(ActionProposal, proposal_id, with_for_update=True)
            assert proposal is not None
            now = self._clock.now()
            if error is None:
                proposal.status = Status.EXECUTED
                proposal.executed_at = now
                proposal.result = result or {}
                event, summary = "action.executed", f"Done: {proposal.summary or kind}"
            else:
                proposal.status = Status.UNKNOWN_OUTCOME if outcome_unknown else Status.FAILED
                proposal.error = error
                event, summary = "action.failed", f"Failed: {proposal.summary or kind}"
            await self._audit.append(
                session,
                actor="system",
                event_type=event,
                subject_type="action",
                subject_id=str(proposal_id),
                summary=summary,
                data={"kind": kind, "status": proposal.status},
            )

    # --- Housekeeping ----------------------------------------------------------------

    async def recover_interrupted(self, session: AsyncSession) -> int:
        """At startup: anything left `executing` crashed mid-run. Never retry it blindly."""
        rows = list(
            await session.scalars(
                select(ActionProposal)
                .where(ActionProposal.status == Status.EXECUTING)
                .with_for_update()
            )
        )
        for proposal in rows:
            proposal.status = Status.UNKNOWN_OUTCOME
            proposal.status_reason = (
                "Jarvis restarted while this was running. "
                "Check whether it happened before retrying."
            )
            await self._audit.append(
                session,
                actor="system",
                event_type="action.unknown_outcome",
                subject_type="action",
                subject_id=str(proposal.id),
                summary=f"Interrupted: {proposal.summary or proposal.kind}",
                data={"kind": proposal.kind},
            )
        return len(rows)

    async def expire_stale(
        self, session: AsyncSession, *, max_age: timedelta = timedelta(days=7)
    ) -> int:
        cutoff = self._clock.now() - max_age
        rows = list(
            await session.scalars(
                select(ActionProposal)
                .where(ActionProposal.status == Status.PENDING)
                .where(ActionProposal.created_at < cutoff)
                .with_for_update(skip_locked=True)
            )
        )
        for proposal in rows:
            proposal.status = Status.EXPIRED
            proposal.status_reason = "Waited more than 7 days for approval"
            await self._audit.append(
                session,
                actor="system",
                event_type="action.expired",
                subject_type="action",
                subject_id=str(proposal.id),
                summary=f"Expired: {proposal.summary or proposal.kind}",
                data={"kind": proposal.kind},
            )
        return len(rows)

    # --- Helpers ----------------------------------------------------------------------

    def _execute_after(self, kind: str, policy: ActionKindPolicy, now: datetime) -> datetime:
        at = now + timedelta(seconds=self.config.undo_window(kind))
        quiet = self.config.defaults.quiet_hours
        if policy.defer_during_quiet_hours and quiet is not None:
            local = at.astimezone(self.config.tz)
            if quiet.contains(local.time()):
                at = quiet.next_end(local)
        return at

    async def _cap_reached(
        self, session: AsyncSession, kind: str, policy: ActionKindPolicy, now: datetime
    ) -> bool:
        if policy.max_per_day is None:
            return False
        count = await session.scalar(
            select(func.count())
            .select_from(ActionProposal)
            .where(ActionProposal.kind == kind)
            .where(ActionProposal.status.in_([s.value for s in _CAP_STATUSES]))
            .where(ActionProposal.decided_at >= now - timedelta(hours=24))
        )
        return (count or 0) >= policy.max_per_day
