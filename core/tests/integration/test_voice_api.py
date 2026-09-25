"""The voice API: satellite pairing, status, and a real conversation over the socket.

The socket test runs the app on a real server and streams recorded speech
(espeak, see tests/fixtures/audio) through the real pipeline: Silero VAD and
Smart Turn decide when the owner has finished, Jarvis answers with the fake
model, and a fake speech server stands in for faster-whisper and Kokoro.
"""

from __future__ import annotations

import asyncio
import json
import socket
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import func, select
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from jarvis.api.app import create_app
from jarvis.api.deps import AppState
from jarvis.clock import SystemClock
from jarvis.db.models import AuditEvent, ChatMessage, Conversation, VoiceDevice
from jarvis.db.session import SessionFactory, transaction
from jarvis.memory.embeddings import HashEmbedder
from jarvis.services import Services, build_services
from jarvis.voice import bench
from tests.conftest import fake_models_config, make_settings
from tests.integration.api_helpers import Harness, login, register, step_up
from tests.integration.fakes import FakeSpeech

pytestmark = pytest.mark.db

AUDIO = Path(__file__).resolve().parents[1] / "fixtures" / "audio"


# --- Pairing and status (httpx harness from test_api) -------------------------------


async def test_pairing_a_satellite_needs_a_passkey_tap(harness: Harness) -> None:
    await register(harness)
    await login(harness)
    denied = await harness.client.post("/api/voice/pairing-code")
    assert denied.status_code == 403
    await step_up(harness)
    made = await harness.client.post("/api/voice/pairing-code")
    assert made.status_code == 200
    code = made.json()["code"]
    assert len(code) == 9
    assert code[4] == "-"

    # The satellite isn't a browser: no Origin header, no cookie.
    satellite = harness.client.build_request(
        "POST", "/api/voice/devices/pair", json={"code": "WRNG-CODE", "name": "Desk"}
    )
    satellite.headers.pop("origin", None)
    assert (await harness.client.send(satellite)).status_code == 403

    good = harness.client.build_request(
        "POST", "/api/voice/devices/pair", json={"code": code.lower(), "name": "Desk PC"}
    )
    good.headers.pop("origin", None)
    paired = await harness.client.send(good)
    assert paired.status_code == 200, paired.text
    token = paired.json()["token"]
    assert token.startswith("jv_")

    # Single use.
    again = harness.client.build_request(
        "POST", "/api/voice/devices/pair", json={"code": code, "name": "Other"}
    )
    again.headers.pop("origin", None)
    assert (await harness.client.send(again)).status_code == 403

    async with harness.services.session_factory() as session:
        device = (await session.scalars(select(VoiceDevice))).one()
        assert device.token_hash != token  # only a hash is stored
        assert await harness.state.voice.devices.authenticate(session, token) is not None

    removed = await harness.client.delete(f"/api/voice/devices/{device.id}")
    assert removed.json() == {"ok": True}
    async with harness.services.session_factory() as session:
        assert await harness.state.voice.devices.authenticate(session, token) is None


async def test_pairing_codes_expire(harness: Harness) -> None:
    await register(harness)
    await login(harness)
    await step_up(harness)
    code = (await harness.client.post("/api/voice/pairing-code")).json()["code"]
    harness.clock.advance(minutes=11)
    late = harness.client.build_request(
        "POST", "/api/voice/devices/pair", json={"code": code, "name": "Desk"}
    )
    late.headers.pop("origin", None)
    assert (await harness.client.send(late)).status_code == 403


async def test_voice_status(harness: Harness) -> None:
    assert (await harness.client.get("/api/voice/status")).status_code == 401
    await register(harness)
    await login(harness)
    harness.state.voice.speech = FakeSpeech("")  # type: ignore[assignment]
    status = (await harness.client.get("/api/voice/status")).json()
    assert status["enabled"] is True
    assert status["problem"] is None
    assert status["speech"] == {
        "reachable": True,
        "stt_ready": True,
        "tts_ready": True,
        "detail": "ready",
        "key_refused": False,
    }
    assert status["voice"] == "bm_george"
    assert status["confirm_phrase"] == "confirm"
    preview = await harness.client.post("/api/voice/preview")
    assert preview.headers["content-type"] == "audio/mpeg"


# --- A real conversation over the socket ------------------------------------------------


@dataclass
class LiveServer:
    app: FastAPI
    url: str

    @property
    def state(self) -> AppState:
        return self.app.state.jarvis

    @property
    def services(self) -> Services:
        return self.state.services


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def live(session_factory: SessionFactory) -> AsyncIterator[LiveServer]:
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    settings = make_settings(JARVIS_ENABLE_SCHEDULER=False, JARVIS_PUBLIC_ORIGIN=origin)

    def factory(s: Any, sf: SessionFactory) -> Services:
        return build_services(
            s,
            sf,
            clock=SystemClock(),
            embedder=HashEmbedder(),
            models_config=fake_models_config(),
        )

    app = create_app(settings, services_factory=factory, run_background=False)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "the test server did not start"
    try:
        yield LiveServer(app=app, url=f"ws://127.0.0.1:{port}/api/voice/ws")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


async def _device_token(live: LiveServer) -> str:
    devices = live.state.voice.devices
    async with transaction(live.services.session_factory) as session:
        code = await devices.create_pairing_code(session)
    async with transaction(live.services.session_factory) as session:
        _, token = await devices.pair(session, code.code, "Test satellite")
    return token


def _pcm(name: str) -> bytes:
    with wave.open(str(AUDIO / name)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16_000, 1, 2)
        return w.readframes(w.getnframes())


async def _speak(ws: ClientConnection, pcm: bytes, *, silence_after: float = 2.0) -> None:
    """Send audio as a microphone would: 20 ms chunks, in real time."""
    chunk = 640
    audio = pcm + b"\x00\x00" * int(16_000 * silence_after)
    for i in range(0, len(audio), chunk):
        await ws.send(audio[i : i + chunk])
        await asyncio.sleep(0.02)


async def _collect(
    ws: ClientConnection, until: Any, seconds: float = 25.0
) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    audio_bytes = 0
    async with asyncio.timeout(seconds):
        while True:
            message = await ws.recv()
            if isinstance(message, bytes):
                audio_bytes += len(message)
                continue
            events.append(json.loads(message))
            if until(events):
                return events, audio_bytes


def _reply_finished(events: list[dict[str, Any]]) -> bool:
    """The reply's text is complete and Jarvis has stopped speaking after it."""
    end = {"type": "assistant", "text": "", "final": True}
    if end not in events:
        return False
    return {"type": "state", "state": "idle"} in events[events.index(end) :]


async def test_a_spoken_question_gets_a_spoken_answer(live: LiveServer) -> None:
    speech = FakeSpeech("What is on my calendar today?")
    live.state.voice.speech = speech  # type: ignore[assignment]
    token = await _device_token(live)
    async with connect(live.url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
        ready, _ = await _collect(ws, lambda ev: ev[-1]["type"] == "ready", seconds=10)
        assert ready[-1] == {"type": "ready", "input_rate": 16_000, "output_rate": 24_000}
        speaking = asyncio.create_task(_speak(ws, _pcm("question.wav")))
        events, audio = await _collect(ws, _reply_finished)
        await speaking

    kinds = [e["type"] for e in events]
    assert {"type": "state", "state": "listening"} in events
    assert {"type": "user", "text": "What is on my calendar today?", "final": True} in events
    reply = "".join(e["text"] for e in events if e["type"] == "assistant")
    assert reply.strip() == "Understood. You said: What is on my calendar today?"
    assert {"type": "state", "state": "speaking"} in events
    assert kinds.index("user") < kinds.index("assistant")
    assert audio > 0  # Jarvis's voice came back as PCM
    assert speech.heard
    assert speech.heard[0][:4] == b"RIFF"  # a WAV of the utterance
    assert "".join(speech.said).strip().startswith("Understood.")

    async with live.services.session_factory() as session:
        messages = list(await session.scalars(select(ChatMessage).order_by(ChatMessage.created_at)))
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_voice_explains_when_it_cannot_run(live: LiveServer) -> None:
    live.state.voice.speech = FakeSpeech("")  # type: ignore[assignment]
    live.state.voice.text_data_problem = "Voice is missing its sentence data. " + "x" * 200
    token = await _device_token(live)
    async with connect(live.url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), timeout=5)
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 4503
    assert closed.value.rcvd.reason.startswith("Voice is missing its sentence data.")


async def test_the_socket_refuses_strangers(live: LiveServer) -> None:
    live.state.voice.speech = FakeSpeech("")  # type: ignore[assignment]
    # A page on another site can't open it (cross-site WebSocket hijacking).
    with pytest.raises(InvalidStatus):
        async with connect(live.url, origin="https://evil.example"):  # type: ignore[arg-type]
            pass
    # No cookie and no device token: closed with a clear reason.
    async with connect(live.url) as ws:
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), timeout=5)
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 4401
    # A revoked or made-up token is refused too.
    async with connect(live.url, additional_headers={"Authorization": "Bearer jv_nope"}) as ws:
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), timeout=5)
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 4401


# --- The latency benchmark (make bench-voice) ------------------------------------------


async def _leftovers(live: LiveServer) -> tuple[VoiceDevice | None, int, int, set[str]]:
    async with live.services.session_factory() as session:
        device = await session.scalar(select(VoiceDevice).where(VoiceDevice.name == bench.NAME))
        conversations = await session.scalar(select(func.count()).select_from(Conversation))
        messages = await session.scalar(select(func.count()).select_from(ChatMessage))
        actors = await session.scalars(
            select(AuditEvent.actor).where(AuditEvent.event_type.like("voice.device_%"))
        )
        return device, conversations or 0, messages or 0, set(actors)


async def test_the_benchmark_times_each_turn_then_cleans_up(live: LiveServer) -> None:
    live.state.voice.speech = FakeSpeech("What is on my calendar today?")  # type: ignore[assignment]
    report = await bench.run_benchmark(
        live.services.session_factory, clock=SystemClock(), url=live.url, runs=2
    )
    assert len(report.turns) == 2
    for turn in report.turns:
        assert turn.finished
        assert not turn.cut_off
        assert turn.heard is not None
        assert turn.answered is not None
        assert turn.speaking is not None
        assert 0 < turn.heard <= turn.answered <= turn.speaking
    assert report.render().startswith("2 turns")
    device, conversations, messages, actors = await _leftovers(live)
    assert device is not None
    assert device.revoked_at is not None  # its token no longer opens the socket
    assert (conversations, messages) == (0, 0)  # nothing left in your chats
    assert actors == {"benchmark"}  # the audit log says who added and removed it


async def test_the_benchmark_explains_a_busy_socket_and_still_cleans_up(
    live: LiveServer,
) -> None:
    live.state.voice.speech = FakeSpeech("")  # type: ignore[assignment]
    live.state.voice.max_sessions = 1
    token = await _device_token(live)
    async with connect(live.url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
        await _collect(ws, lambda ev: ev[-1]["type"] == "ready", seconds=10)  # already talking
        with pytest.raises(bench.BenchError, match="4429"):
            await bench.run_benchmark(
                live.services.session_factory, clock=SystemClock(), url=live.url, runs=1
            )
    device, conversations, _, _ = await _leftovers(live)
    assert device is not None
    assert device.revoked_at is not None
    assert conversations == 0
