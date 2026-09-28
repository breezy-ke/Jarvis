"""Your tech brief in the app: today's and past ones, your 👍/👎, and the audio version.

Every link in a brief is its source's own address. Audio files are served by
name from Jarvis's data folder, never by path.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jarvis.api.deps import Owner, State
from jarvis.brief.service import BriefNotFound, BriefService

router = APIRouter(prefix="/api/brief", tags=["brief"])


def _brief(state: State) -> BriefService:
    if state.brief is None:
        raise HTTPException(status_code=503, detail=state.brief_problem or "The brief is off.")
    return state.brief


@router.get("/status")
async def status(owner: Owner, state: State) -> dict[str, Any]:
    if state.brief is None:
        return {"enabled": False, "problem": state.brief_problem}
    brief = state.brief
    day = brief.today()
    today = await brief.brief_for(day)
    return {
        "enabled": True,
        "problem": None,
        "time": brief.config.time,
        "timezone": str(brief.tz),
        "today": {
            "day": day.isoformat(),
            "status": today.status if today else None,
            "on_time": today.on_time if today else None,
            "deliveries": today.deliveries if today else {},
        },
        "next_delivery": brief.next_delivery(today.status if today else None).isoformat(),
        "making": brief.making,
        "streak": await brief.streak(),
        "last_error": brief.last_error,
        "channels": brief.config.channels.model_dump(),
        "sources": await brief.source_health(),
    }


@router.get("")
async def get_brief(owner: Owner, state: State, day: date | None = None) -> dict[str, Any]:
    """Today's brief, or the one for `day`."""
    try:
        view = await _brief(state).view(day=day)
    except BriefNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return view.to_json()


@router.get("/history")
async def history(
    owner: Owner, state: State, limit: int = Query(default=30, ge=1, le=90)
) -> list[dict[str, Any]]:
    return await _brief(state).history(limit=limit)


@router.post("/make", status_code=202)
async def make(owner: Owner, state: State) -> dict[str, bool]:
    """Make today's brief now (it's delivered straight away once 07:00 has passed)."""
    brief = _brief(state)
    started = brief.start_making()
    return {"making": True, "already": not started}


class VoteIn(BaseModel):
    vote: Literal[1, -1] | None


@router.post("/entries/{entry_id}/vote")
async def vote(entry_id: uuid.UUID, body: VoteIn, owner: Owner, state: State) -> dict[str, Any]:
    entry = await _brief(state).vote(entry_id, body.vote)
    if entry is None:
        raise HTTPException(status_code=404, detail="That story isn't in Jarvis any more.")
    return {"id": str(entry.id), "vote": entry.vote}


@router.get("/audio/{name}")
async def audio(name: str, owner: Owner, state: State) -> FileResponse:
    path = _brief(state).audio_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="No such recording.")
    return FileResponse(
        path, media_type="audio/mpeg", filename=name, content_disposition_type="inline"
    )
