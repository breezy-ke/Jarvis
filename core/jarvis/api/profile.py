"""'What Jarvis knows about me': profile, memories, suggestions and export."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from jarvis.api.deps import Owner, State
from jarvis.db.models import Episode, Fact
from jarvis.db.session import transaction
from jarvis.ingestion.service import SUGGESTION, IngestionError
from jarvis.memory.store import CATEGORIES, FactInput, FactStatus, MemoryStoreError
from jarvis.profile.schema import REQUIRED_FIELDS, PathError, Profile
from jarvis.profile.service import SignOffError, completeness

router = APIRouter(prefix="/api", tags=["profile"])


async def _profile_payload(state: State) -> dict[str, Any]:
    async with state.services.session_factory() as session:
        snapshot = await state.services.profiles.current(session)
        gate = await state.services.profiles.status(session)
    score = completeness(snapshot.profile)
    return {
        "version": snapshot.version,
        "data": snapshot.data,
        "schema": Profile.model_json_schema(),
        "required_fields": list(REQUIRED_FIELDS),
        "completeness": round(score.score, 3),
        "missing_required": score.missing,
        "signed_off_at": snapshot.signed_off_at.isoformat() if snapshot.signed_off_at else None,
        "ever_signed_off": snapshot.ever_signed_off,
        "gate": {"open": gate.open, "reason": gate.reason},
    }


@router.get("/profile")
async def get_profile(owner: Owner, state: State) -> dict[str, Any]:
    return await _profile_payload(state)


class ProfilePatch(BaseModel):
    changes: dict[str, Any] = Field(min_length=1, max_length=50)


@router.patch("/profile")
async def patch_profile(body: ProfilePatch, owner: Owner, state: State) -> dict[str, Any]:
    try:
        async with transaction(state.services.session_factory) as session:
            await state.services.profiles.update(
                session, body.changes, created_by="owner", note="edited"
            )
    except PathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _profile_payload(state)


@router.post("/profile/sign-off")
async def sign_off(owner: Owner, state: State) -> dict[str, Any]:
    try:
        async with transaction(state.services.session_factory) as session:
            await state.services.profiles.sign_off(session)
    except SignOffError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _profile_payload(state)


@router.get("/profile/versions")
async def versions(owner: Owner, state: State) -> list[dict[str, Any]]:
    async with state.services.session_factory() as session:
        rows = await state.services.profiles.history(session)
    return [
        {
            "version": v.version,
            "created_at": v.created_at.isoformat(),
            "created_by": v.created_by,
            "note": v.note,
            "signed_off_at": v.signed_off_at.isoformat() if v.signed_off_at else None,
        }
        for v in rows
    ]


def _fact(f: Fact) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "category": f.category,
        "subject": f.subject,
        "predicate": f.predicate,
        "value": f.value,
        "status": f.status,
        "source": f.source,
        "source_ref": f.source_ref,
        "confidence": f.confidence,
        "sensitivity": f.sensitivity,
        "updated_at": f.updated_at.isoformat(),
        "is_suggestion": f.category == SUGGESTION,
    }


@router.get("/memory/facts")
async def facts(
    owner: Owner,
    state: State,
    status: str = Query(default="active", pattern="^(active|inferred|confirmed)$"),
    category: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    statuses = {
        "active": (FactStatus.CONFIRMED, FactStatus.INFERRED),
        "inferred": (FactStatus.INFERRED,),
        "confirmed": (FactStatus.CONFIRMED,),
    }[status]
    async with state.services.session_factory() as session:
        rows = await state.services.memory.list(
            session, statuses=statuses, category=category, query=q, limit=limit
        )
    return [_fact(f) for f in rows]


class FactIn(BaseModel):
    category: str = Field(default="note")
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=4000)


@router.post("/memory/facts")
async def add_fact(body: FactIn, owner: Owner, state: State) -> dict[str, Any]:
    if body.category not in CATEGORIES or body.category == SUGGESTION:
        raise HTTPException(status_code=422, detail="Unknown category.")
    try:
        async with transaction(state.services.session_factory) as session:
            fact = await state.services.memory.add(
                session,
                FactInput(
                    category=body.category,
                    subject=body.subject,
                    predicate=body.predicate,
                    value=body.value,
                    source="owner:manual",
                    status=FactStatus.CONFIRMED,
                    confidence=1.0,
                ),
                actor="owner",
            )
            return _fact(fact)
    except MemoryStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class FactEdit(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


@router.patch("/memory/facts/{fact_id}")
async def edit_fact(
    fact_id: uuid.UUID, body: FactEdit, owner: Owner, state: State
) -> dict[str, Any]:
    try:
        async with transaction(state.services.session_factory) as session:
            fact = await state.services.memory.edit(session, fact_id, body.value)
            return _fact(fact)
    except MemoryStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/memory/facts/{fact_id}/confirm")
async def confirm_fact(fact_id: uuid.UUID, owner: Owner, state: State) -> dict[str, bool]:
    async with state.services.session_factory() as session:
        fact = await session.get(Fact, fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="Not found.")
    try:
        if fact.category == SUGGESTION:
            await state.ingestion.accept_suggestion(fact_id)
        else:
            async with transaction(state.services.session_factory) as session:
                await state.services.memory.confirm(session, fact_id)
    except (IngestionError, MemoryStoreError, PathError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/memory/facts/{fact_id}/reject")
async def reject_fact(fact_id: uuid.UUID, owner: Owner, state: State) -> dict[str, bool]:
    try:
        async with transaction(state.services.session_factory) as session:
            await state.services.memory.reject(session, fact_id)
    except MemoryStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/memory/facts/{fact_id}")
async def delete_fact(fact_id: uuid.UUID, owner: Owner, state: State) -> dict[str, bool]:
    async with transaction(state.services.session_factory) as session:
        found = await state.services.memory.forget(session, fact_id, hard=True)
    if not found:
        raise HTTPException(status_code=404, detail="Not found.")
    return {"ok": True}


@router.get("/memory/export")
async def export(owner: Owner, state: State) -> JSONResponse:
    """Everything Jarvis knows about you, as one JSON file (tokens are never included)."""
    s = state.services
    async with s.session_factory() as session:
        snapshot = await s.profiles.current(session)
        fact_rows = list(await session.scalars(select(Fact).order_by(Fact.created_at)))
        episodes = list(await session.scalars(select(Episode).order_by(Episode.created_at)))
    body = {
        "exported_at": s.clock.now().isoformat(),
        "profile": {"version": snapshot.version, "data": snapshot.data},
        "facts": [
            {**_fact(f), "valid_to": f.valid_to.isoformat() if f.valid_to else None}
            for f in fact_rows
        ],
        "episodes": [
            {"summary": e.summary, "created_at": e.created_at.isoformat()} for e in episodes
        ],
    }
    async with transaction(s.session_factory) as session:
        await s.audit.append(
            session, actor="owner", event_type="memory.exported", summary="Exported memory"
        )
    return JSONResponse(
        body, headers={"Content-Disposition": 'attachment; filename="jarvis-memory.json"'}
    )
