"""Voice API: the live voice socket, satellite pairing, and the speech server status."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Response, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState

from jarvis.api.deps import AppState, Owner, State
from jarvis.auth.service import SESSION_COOKIE
from jarvis.db.models import Conversation
from jarvis.db.session import transaction
from jarvis.security.pairing import PairingError
from jarvis.voice.brain import VoiceSessionState
from jarvis.voice.pipeline import run_voice_session
from jarvis.voice.runtime import VoiceRuntime
from jarvis.voice.speech import SpeechError

log = logging.getLogger("jarvis.voice")
router = APIRouter(prefix="/api/voice", tags=["voice"])

PREVIEW_TEXT = "Good evening. I'm ready whenever you are."

# WebSocket close codes the clients understand.
CLOSE_UNAUTHORIZED = 4401
CLOSE_BUSY = 4429
CLOSE_UNAVAILABLE = 4503


def _voice(state: AppState) -> VoiceRuntime:
    return state.voice


@router.get("/status")
async def voice_status(owner: Owner, state: State) -> dict[str, Any]:
    voice = _voice(state)
    async with state.services.session_factory() as session:
        devices = await voice.devices.list_devices(session)
    device_list = [
        {
            "id": str(d.id),
            "name": d.name,
            "created_at": d.created_at.isoformat(),
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
        }
        for d in devices
    ]
    if voice.config is None or voice.speech is None:
        return {"enabled": False, "error": voice.config_error, "devices": device_list}
    speech = await voice.speech.status()
    return {
        "enabled": True,
        "problem": voice.problem,
        "speech": {
            "reachable": speech.reachable,
            "stt_ready": speech.stt_ready,
            "tts_ready": speech.tts_ready,
            "detail": speech.detail,
        },
        "voice": voice.config.speech.voice,
        "stt_model": voice.config.speech.stt_model,
        "language": voice.config.speech.language,
        "confirm_phrase": voice.config.confirm_phrases[0],
        "cancel_phrase": voice.config.cancel_phrases[0],
        "devices": device_list,
    }


@router.post("/preview")
async def voice_preview(owner: Owner, state: State) -> Response:
    """A short sample of Jarvis's voice (MP3), to try voices in Settings."""
    voice = _voice(state)
    if voice.speech is None:
        raise HTTPException(status_code=503, detail=voice.config_error or "Voice is off.")
    try:
        audio = await voice.speech.synthesize(PREVIEW_TEXT)
    except SpeechError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(audio, media_type="audio/mpeg")


@router.post("/pairing-code")
async def pairing_code(owner: Owner, state: State) -> dict[str, str]:
    """A one-time code for pairing a voice satellite. Needs a fresh passkey tap."""
    async with transaction(state.services.session_factory) as session:
        if not await state.auth.consume_step_up(session, owner):
            raise HTTPException(status_code=403, detail="Confirm with your passkey first.")
        code = await _voice(state).devices.create_pairing_code(session)
    return {"code": code.code, "expires_at": code.expires_at.isoformat()}


class PairBody(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    name: str = Field(default="Voice satellite", max_length=80)


@router.post("/devices/pair")
async def pair_device(body: PairBody, state: State) -> dict[str, str]:
    """Trade a pairing code for a device token (called by the satellite, not a browser)."""
    try:
        async with transaction(state.services.session_factory) as session:
            device, token = await _voice(state).devices.pair(session, body.code, body.name)
    except PairingError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"device_id": str(device.id), "name": device.name, "token": token}


@router.delete("/devices/{device_id}")
async def remove_device(device_id: uuid.UUID, owner: Owner, state: State) -> dict[str, bool]:
    async with transaction(state.services.session_factory) as session:
        removed = await _voice(state).devices.revoke(session, device_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No such device.")
    return {"ok": True}


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _short(reason: str) -> str:
    """WebSocket close reasons are limited to 123 bytes of UTF-8."""
    data = reason.encode()
    return reason if len(data) <= 123 else data[:120].decode(errors="ignore") + "…"


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    if websocket.client_state == WebSocketState.CONNECTING:
        await websocket.accept()
    await websocket.close(code=code, reason=_short(reason))


@router.websocket("/ws")
async def voice_socket(websocket: WebSocket) -> None:
    """A live voice conversation. See jarvis/voice/protocol.py for the protocol."""
    state: AppState = websocket.app.state.jarvis
    services = state.services
    voice = _voice(state)
    origin = websocket.headers.get("origin")
    if origin is not None and origin != services.settings.public_origin:
        await websocket.close()  # a page on another site: refuse the handshake
        return

    channel: str | None = None
    token = _bearer(websocket.headers.get("authorization"))
    async with transaction(services.session_factory) as session:
        if token is not None:
            if await voice.devices.authenticate(session, token) is not None:
                channel = "satellite"
        elif origin is not None:
            cookie = websocket.cookies.get(SESSION_COOKIE)
            if await state.auth.resolve(session, cookie) is not None:
                channel = "voice"
    if channel is None:
        await _close(websocket, CLOSE_UNAUTHORIZED, "Sign in first.")
        return
    if voice.config is None or voice.speech is None or voice.problem:
        await _close(websocket, CLOSE_UNAVAILABLE, voice.problem or "Voice is off.")
        return
    if voice.active_sessions >= voice.max_sessions:
        await _close(websocket, CLOSE_BUSY, "Another voice session is already open.")
        return

    conversation_id: uuid.UUID | None = None
    if raw := websocket.query_params.get("conversation"):
        try:
            conversation_id = uuid.UUID(raw)
        except ValueError:
            conversation_id = None
        if conversation_id is not None:
            async with services.session_factory() as session:
                if await session.get(Conversation, conversation_id) is None:
                    conversation_id = None

    await websocket.accept()
    voice.active_sessions += 1
    log.info("voice session opened (%s)", channel)
    try:
        await run_voice_session(
            websocket,
            services,
            state.chat,
            voice.config,
            voice.speech,
            channel=channel,
            state=VoiceSessionState(conversation_id=conversation_id),
        )
    finally:
        voice.active_sessions -= 1
        log.info("voice session closed (%s)", channel)
