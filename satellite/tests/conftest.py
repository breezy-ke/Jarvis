"""Stand-ins for the microphone, speaker, wake word, tray and Jarvis itself."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import numpy as np
import pytest
from websockets.asyncio.server import ServerConnection, serve

from jarvis_satellite.config import Settings
from jarvis_satellite.pairing import MemoryStore

TOKEN = "jv_" + "t" * 43

SILENCE, SPEECH, WAKE = 0, 1000, 7777


def frame(value: int) -> np.ndarray:
    return np.full(1280, value, dtype=np.int16)


class FakeMic:
    """A microphone the test speaks into, one 80 ms frame at a time."""

    def __init__(self, *, missing: int = 0) -> None:
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.missing = missing  # how many more times opening it fails (not plugged in yet)

    def say(self, value: int, count: int = 1) -> None:
        for _ in range(count):
            self.queue.put_nowait(frame(value))

    async def frames(self) -> AsyncIterator[np.ndarray]:
        if self.missing:
            self.missing -= 1
            raise OSError("no microphone found")
        while True:
            yield await self.queue.get()


class FakeWake:
    def __init__(self) -> None:
        self.fed = 0
        self.resets = 0

    def detect(self, audio: np.ndarray) -> bool:
        self.fed += 1
        return int(audio[0]) == WAKE

    def reset(self) -> None:
        self.resets += 1


class FakeSpeaker:
    """Plays in real time: busy for as long as the audio it was given lasts."""

    def __init__(self) -> None:
        self.played = 0
        self.chimes = 0
        self.flushes = 0
        self._until = 0.0

    @property
    def playing(self) -> bool:
        return time.monotonic() < self._until

    def play(self, pcm: bytes) -> None:
        self.played += len(pcm)
        start = max(time.monotonic(), self._until)
        self._until = start + len(pcm) / 2 / 24_000

    def flush(self) -> None:
        self.flushes += 1
        self._until = 0.0

    def chime(self) -> None:
        self.chimes += 1


class FakeIndicator:
    def __init__(self) -> None:
        self.states: list[tuple[str, str | None]] = []

    def show(self, state: str, detail: str | None = None) -> None:
        self.states.append((state, detail))

    @property
    def names(self) -> list[str]:
        return [s for s, _ in self.states]

    @property
    def last(self) -> tuple[str, str | None]:
        return self.states[-1] if self.states else ("", None)


class FakeJarvis:
    """Speaks Jarvis's voice socket protocol (core/jarvis/voice/protocol.py)."""

    def __init__(self) -> None:
        self.connections = 0
        self.open = 0
        self.audio: list[bytes] = []
        self.controls: list[dict[str, Any]] = []
        self.authorization: list[str | None] = []
        self.reply_after = 5  # speech frames heard before Jarvis answers
        self.speak_seconds = 0.4
        self.confirmation = False
        self.ready_delay = 0.0
        self.close_with: tuple[int, str] | None = None
        self.url = ""

    def speech_heard(self) -> int:
        return sum(1 for chunk in self.audio if np.frombuffer(chunk, "<i2")[0] == SPEECH)

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.open += 1
        try:
            auth = ws.request.headers.get("Authorization") if ws.request else None
            self.authorization.append(auth)
            if auth != f"Bearer {TOKEN}":
                await ws.close(4401, "Sign in first.")
                return
            if self.close_with:
                await ws.close(*self.close_with)
                return
            await asyncio.sleep(self.ready_delay)
            await self.event(ws, type="ready", input_rate=16000, output_rate=24000)
            await self.event(ws, type="state", state="idle")
            answered = False
            replies: set[asyncio.Task[None]] = set()
            async for message in ws:
                if isinstance(message, str):
                    self.controls.append(json.loads(message))
                    continue
                self.audio.append(message)
                if not answered and self.speech_heard() >= self.reply_after:
                    answered = True
                    reply = asyncio.create_task(self.answer(ws))
                    replies.add(reply)
                    reply.add_done_callback(replies.discard)
        finally:
            self.open -= 1

    async def event(self, ws: ServerConnection, **data: Any) -> None:
        await ws.send(json.dumps(data))

    async def answer(self, ws: ServerConnection) -> None:
        await self.event(ws, type="state", state="listening")
        await self.event(ws, type="user", text="What is on my calendar today?", final=True)
        await self.event(ws, type="state", state="thinking")
        await self.event(ws, type="state", state="speaking")
        await ws.send(b"\x00\x00" * int(24_000 * self.speak_seconds))
        await self.event(ws, type="assistant", text="Two meetings.", final=True)
        if self.confirmation:
            await self.event(ws, type="confirmation", proposal_id="p1", summary="Invite to Kickoff")
        await asyncio.sleep(self.speak_seconds)
        await self.event(ws, type="state", state="idle")


class ScriptedJarvis:
    """Jarvis's voice socket, driven message by message by the test.

    For timing a real server can't pin down: an event queued here is like one
    already on the wire, so the satellite reads it even if it hangs up first.
    Pass it as the satellite's connector.
    """

    def __init__(self) -> None:
        self.connections = 0
        self.sent: list[str | bytes] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()

    def event(self, **data: Any) -> None:
        self._incoming.put_nowait(json.dumps(data))

    def __call__(self, url: str, **options: Any) -> ScriptedJarvis:  # connect(url, ...)
        self.connections += 1
        return self

    async def __aenter__(self) -> ScriptedJarvis:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def send(self, data: str | bytes) -> None:
        assert not self.closed, "sent on a closed socket"
        self.sent.append(data)

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.close_code = 1000
            self._incoming.put_nowait(None)  # after whatever is already on the wire

    def __aiter__(self) -> AsyncIterator[str]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[str]:
        while (message := await self._incoming.get()) is not None:
            yield message


@pytest.fixture
async def jarvis() -> AsyncIterator[FakeJarvis]:
    fake = FakeJarvis()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        fake.url = f"http://127.0.0.1:{port}"
        yield fake


@pytest.fixture
def tokens(jarvis: FakeJarvis) -> MemoryStore:
    store = MemoryStore()
    store.set(jarvis.url, TOKEN)
    return store


@pytest.fixture
def settings(jarvis: FakeJarvis) -> Settings:
    return Settings(server=jarvis.url, name="Test PC", follow_up_seconds=0.4, confirm_seconds=1.5)


async def eventually(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it didn't happen in time")
