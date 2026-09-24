"""Onboarding interview, consented ingestion sources, and the Google connection."""

from __future__ import annotations

from dataclasses import asdict
from html import escape
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from jarvis.api.deps import AppState, Owner, State
from jarvis.api.sse import sse
from jarvis.db.session import transaction
from jarvis.ingestion.documents import MAX_UPLOAD_BYTES
from jarvis.ingestion.google import GoogleError
from jarvis.ingestion.service import IngestionError
from jarvis.onboarding.modules import MODULES_BY_ID

router = APIRouter(prefix="/api", tags=["onboarding"])


@router.get("/onboarding")
async def overview(owner: Owner, state: State) -> dict[str, Any]:
    statuses = await state.onboarding.statuses()
    return {
        **(await state.onboarding.overview()),
        "modules": [
            {**asdict(s), "opening": MODULES_BY_ID[s.id].opening, "goal": MODULES_BY_ID[s.id].goal}
            for s in statuses
        ],
    }


@router.get("/onboarding/{module_id}/transcript")
async def transcript(module_id: str, owner: Owner, state: State) -> list[dict[str, str]]:
    try:
        return await state.onboarding.transcript(module_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown module.") from exc


class TurnIn(BaseModel):
    text: str | None = Field(default=None, max_length=8_000)


@router.post("/onboarding/{module_id}/message")
async def message(module_id: str, body: TurnIn, owner: Owner, state: State) -> EventSourceResponse:
    if module_id not in MODULES_BY_ID:
        raise HTTPException(status_code=404, detail="Unknown module.")
    return sse(state.onboarding.stream_turn(module_id, body.text))


@router.post("/onboarding/{module_id}/skip")
async def skip(module_id: str, owner: Owner, state: State) -> dict[str, bool]:
    try:
        await state.onboarding.skip(module_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown module.") from exc
    return {"ok": True}


# --- Ingestion ----------------------------------------------------------------------------


@router.get("/ingestion/sources")
async def sources(owner: Owner, state: State) -> list[dict[str, Any]]:
    return await state.ingestion.list_sources()


class ConsentIn(BaseModel):
    consent: bool
    settings: dict[str, str] | None = None


@router.post("/ingestion/{source}/consent")
async def consent(source: str, body: ConsentIn, owner: Owner, state: State) -> dict[str, bool]:
    try:
        await state.ingestion.set_consent(source, consent=body.consent, settings=body.settings)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/ingestion/{source}/run")
async def run(source: str, owner: Owner, state: State) -> dict[str, Any]:
    try:
        return await state.ingestion.run(source)
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/ingestion/documents/upload")
async def upload(owner: Owner, state: State, file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Files must be under 10 MB.")
    try:
        return await state.ingestion.ingest_upload(file.filename or "upload", data)
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --- Google ----------------------------------------------------------------------------


@router.get("/integrations/google")
async def google_status(owner: Owner, state: State) -> dict[str, Any]:
    async with state.services.session_factory() as session:
        connection = await state.google.connection(session)
    return {
        "configured": state.google.configured,
        "connected": connection.connected,
        "account_email": connection.account_email,
        "scopes": connection.scopes,
        "redirect_uri": state.google.redirect_uri,
    }


@router.post("/integrations/google/connect")
async def google_connect(owner: Owner, state: State) -> dict[str, str]:
    try:
        async with transaction(state.services.session_factory) as session:
            url = await state.google.start(session)
    except GoogleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"auth_url": url}


def _page(title: str, message: str, ok: bool) -> HTMLResponse:
    color = "#16a34a" if ok else "#dc2626"
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>Jarvis</title></head>"
        "<body style='font-family:system-ui;background:#0b0d12;color:#e6e8ee;display:grid;"
        "place-items:center;min-height:100vh;margin:0'><main style='max-width:28rem;padding:2rem'>"
        f"<h1 style='color:{color};font-size:1.25rem'>{escape(title)}</h1>"
        f"<p>{escape(message)}</p></main></body></html>"
    )
    return HTMLResponse(html, status_code=200 if ok else 400)


@router.get("/integrations/google/callback", include_in_schema=False)
async def google_callback(request: Request, app_state: State) -> HTMLResponse:
    """Google redirects here (loopback). Protected by the single-use OAuth `state` value."""
    return await handle_google_callback(app_state, dict(request.query_params))


@router.post("/integrations/google/disconnect")
async def google_disconnect(owner: Owner, state: State) -> dict[str, bool]:
    async with transaction(state.services.session_factory) as session:
        await state.google.disconnect(session)
        await state.services.audit.append(
            session, actor="owner", event_type="integration.google", summary="Disconnected Google"
        )
    return {"ok": True}


async def handle_google_callback(state_obj: AppState, query: dict[str, str]) -> HTMLResponse:
    if query.get("error"):
        return _page("Google sign-in cancelled", f"Google said: {query['error']}.", ok=False)
    code, oauth_state = query.get("code"), query.get("state")
    if not code or not oauth_state:
        return _page("Missing details", "This page needs the code Google sends back.", ok=False)
    try:
        async with transaction(state_obj.services.session_factory) as session:
            connection = await state_obj.google.finish(session, state=oauth_state, code=code)
            await state_obj.services.audit.append(
                session,
                actor="owner",
                event_type="integration.google",
                summary="Connected Google (read-only)",
            )
    except GoogleError as exc:
        return _page("Couldn't connect Google", str(exc), ok=False)
    who = connection.account_email or "your Google account"
    return _page(
        "Google connected",
        f"Jarvis can now read {who} (read-only). You can close this tab.",
        ok=True,
    )
