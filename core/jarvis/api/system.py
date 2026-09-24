"""Dashboard status, kill switch, models and quotas, policies, audit log, push."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from jarvis import __version__
from jarvis.api.deps import Owner, State
from jarvis.db.models import ActionProposal, AuditEvent, Fact
from jarvis.db.session import transaction
from jarvis.llm.router import RouterError
from jarvis.memory.store import FactStatus
from jarvis.policy.state import get_kill_switch, set_kill_switch
from jarvis.policy.types import Status
from jarvis.profile.service import completeness

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health(state: State) -> dict[str, Any]:
    async with state.services.session_factory() as session:
        await session.execute(select(1))
    return {"ok": True, "version": __version__}


@router.get("/status")
async def status(owner: Owner, state: State) -> dict[str, Any]:
    s = state.services
    async with s.session_factory() as session:
        kill = await get_kill_switch(session)
        gate = await s.profiles.status(session)
        snapshot = await s.profiles.current(session)
        pending = await session.scalar(
            select(func.count())
            .select_from(ActionProposal)
            .where(ActionProposal.status == Status.PENDING)
        )
        to_review = await session.scalar(
            select(func.count())
            .select_from(Fact)
            .where(Fact.status == FactStatus.INFERRED)
            .where(Fact.valid_to.is_(None))
        )
    profile = snapshot.profile
    return {
        "version": __version__,
        "owner_name": profile.identity.preferred_name,
        "assistant_name": profile.assistant.name,
        "kill_switch": {"engaged": kill.engaged, "reason": kill.reason},
        "autonomy": {"open": gate.open, "reason": gate.reason},
        "profile": {
            "completeness": round(completeness(profile).score, 3),
            "signed_off": snapshot.ever_signed_off,
            "version": snapshot.version,
        },
        "pending_approvals": pending or 0,
        "memories_to_review": to_review or 0,
        "timezone": s.policies_config.defaults.timezone,
    }


class KillSwitchIn(BaseModel):
    engaged: bool
    reason: str | None = Field(default=None, max_length=300)


@router.post("/system/kill-switch")
async def kill_switch(body: KillSwitchIn, owner: Owner, state: State) -> dict[str, Any]:
    s = state.services
    async with transaction(s.session_factory) as session:
        result = await set_kill_switch(
            session,
            engaged=body.engaged,
            reason=body.reason,
            actor="owner",
            audit=s.audit,
            clock=s.clock,
        )
    return {"engaged": result.engaged, "reason": result.reason}


@router.get("/system/models")
async def models(owner: Owner, state: State) -> dict[str, Any]:
    router_ = state.services.router
    tasks: dict[str, Any] = {}
    for task in router_.config.tasks:
        try:
            statuses = await router_.availability(task)
            tasks[task] = {
                "privacy": router_.effective_privacy(task, None).value,
                "candidates": [
                    {"model": st.candidate.ref, "available": st.available, "reason": st.reason}
                    for st in statuses
                ],
            }
        except RouterError as exc:
            tasks[task] = {"error": str(exc), "candidates": []}
    cfg = router_.config
    return {
        "budget_usd": cfg.budget.monthly_usd_cap,
        "models": {
            ref: {
                "provider": m.provider,
                "model": m.model,
                "local": cfg.providers[m.provider].local,
                "trains_on_data": cfg.providers[m.provider].trains_on_data,
                "zero_data_retention": cfg.providers[m.provider].zero_data_retention,
                "limits": m.limits.model_dump(exclude_none=True),
            }
            for ref, m in cfg.models.items()
        },
        "tasks": tasks,
    }


@router.get("/system/policies")
async def policies(owner: Owner, state: State) -> dict[str, Any]:
    cfg = state.services.policies_config
    available = set(state.services.registry.kinds())
    return {
        "defaults": cfg.defaults.model_dump(mode="json"),
        "approval_channels": {
            k.value: [c.value for c in v] for k, v in cfg.approval_channels.items()
        },
        "action_kinds": {
            kind: {**policy.model_dump(mode="json"), "available": kind in available}
            for kind, policy in cfg.action_kinds.items()
        },
    }


@router.get("/audit")
async def audit(
    owner: Owner,
    state: State,
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if before_id:
        stmt = stmt.where(AuditEvent.id < before_id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type.startswith(event_type))
    async with state.services.session_factory() as session:
        rows = await session.scalars(stmt)
        return [
            {
                "id": e.id,
                "ts": e.ts.isoformat(),
                "actor": e.actor,
                "event_type": e.event_type,
                "summary": e.summary,
                "subject_type": e.subject_type,
                "subject_id": e.subject_id,
                "data": e.data,
                "hash": e.hash,
            }
            for e in rows
        ]


@router.get("/audit/verify")
async def audit_verify(owner: Owner, state: State) -> dict[str, Any]:
    async with state.services.session_factory() as session:
        result = await state.services.audit.verify(session)
    return {
        "ok": result.ok,
        "checked": result.checked,
        "first_bad_id": result.first_bad_id,
        "reason": result.reason,
    }


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000)
    keys: dict[str, str]


@router.get("/push/public-key")
async def push_key(owner: Owner, state: State) -> dict[str, Any]:
    key = state.services.settings.vapid_public_key
    return {"configured": bool(key and state.services.push.configured), "public_key": key}


@router.post("/push/subscribe")
async def push_subscribe(
    body: PushSubscriptionIn, request: Request, owner: Owner, state: State
) -> dict[str, bool]:
    if not {"p256dh", "auth"} <= set(body.keys):
        raise HTTPException(status_code=422, detail="Subscription keys missing.")
    if not body.endpoint.startswith("https://"):
        raise HTTPException(status_code=422, detail="Push endpoints must use https.")
    await state.services.push.subscribe(
        endpoint=body.endpoint,
        keys=body.keys,
        user_agent=request.headers.get("user-agent"),
        now=state.services.clock.now(),
    )
    return {"ok": True}


@router.post("/push/test")
async def push_test(owner: Owner, state: State) -> dict[str, Any]:
    result = await state.services.push.send(
        title="Jarvis", body="Notifications are working.", url="/"
    )
    return {"configured": result.configured, "delivered": result.delivered}
