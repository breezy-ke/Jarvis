"""The approval queue: review, approve (passkey for high risk), reject, undo."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from jarvis.api.deps import Owner, State
from jarvis.db.models import ActionProposal
from jarvis.db.session import transaction
from jarvis.policy.engine import ApprovalError
from jarvis.policy.types import Channel, Status

router = APIRouter(prefix="/api/actions", tags=["actions"])

_OPEN = (Status.PENDING, Status.APPROVED, Status.EXECUTING)


def serialize(p: ActionProposal) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "kind": p.kind,
        "summary": p.summary,
        "status": p.status,
        "status_reason": p.status_reason,
        "risk": p.risk,
        "autonomy": p.autonomy,
        "payload": p.payload,
        "payload_hash": p.payload_hash,
        "rationale": p.rationale,
        "evidence": p.evidence,
        "validation": p.validation,
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat(),
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
        "decided_via": p.decided_via,
        "execute_after": p.execute_after.isoformat() if p.execute_after else None,
        "executed_at": p.executed_at.isoformat() if p.executed_at else None,
        "result": p.result,
        "error": p.error,
        "needs_passkey": p.risk in ("high", "critical"),
    }


class ApproveIn(BaseModel):
    payload_hash: str = Field(min_length=64, max_length=64)
    use_passkey: bool = False


class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.get("")
async def list_actions(
    owner: Owner,
    state: State,
    view: str = Query(default="open", pattern="^(open|history)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    stmt = select(ActionProposal)
    if view == "open":
        stmt = stmt.where(ActionProposal.status.in_([s.value for s in _OPEN]))
    else:
        stmt = stmt.where(ActionProposal.status.not_in([s.value for s in _OPEN]))
    async with state.services.session_factory() as session:
        rows = await session.scalars(stmt.order_by(ActionProposal.created_at.desc()).limit(limit))
        return [serialize(p) for p in rows]


@router.get("/{proposal_id}")
async def get_action(proposal_id: uuid.UUID, owner: Owner, state: State) -> dict[str, Any]:
    async with state.services.session_factory() as session:
        proposal = await session.get(ActionProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return serialize(proposal)


@router.post("/{proposal_id}/approve")
async def approve(
    proposal_id: uuid.UUID, body: ApproveIn, owner: Owner, state: State
) -> dict[str, Any]:
    s = state.services
    try:
        async with transaction(s.session_factory) as session:
            step_up = (
                await state.auth.consume_step_up(session, owner) if body.use_passkey else False
            )
            proposal = await s.policy.approve(
                session,
                proposal_id,
                approved_hash=body.payload_hash,
                channel=Channel.PWA_PASSKEY if body.use_passkey else Channel.PWA,
                step_up_verified=step_up,
            )
            return serialize(proposal)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{proposal_id}/reject")
async def reject(
    proposal_id: uuid.UUID, body: RejectIn, owner: Owner, state: State
) -> dict[str, Any]:
    try:
        async with transaction(state.services.session_factory) as session:
            proposal = await state.services.policy.reject(
                session, proposal_id, channel=Channel.PWA, reason=body.reason
            )
            return serialize(proposal)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{proposal_id}/undo")
async def undo(proposal_id: uuid.UUID, owner: Owner, state: State) -> dict[str, Any]:
    try:
        async with transaction(state.services.session_factory) as session:
            proposal = await state.services.policy.cancel(session, proposal_id, channel=Channel.PWA)
            return serialize(proposal)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
