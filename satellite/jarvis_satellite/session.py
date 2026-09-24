"""The satellite's loop: sleep until “Hey Jarvis”, talk, then go back to sleep.

* Asleep, the microphone only feeds the wake-word model on this PC. Nothing is
  sent anywhere and no connection is open.
* After the wake word, it connects to Jarvis's voice socket and streams the
  microphone, including what you said while it was connecting ("Hey Jarvis,
  what's on today?" in one breath works).
* Half-duplex: while Jarvis speaks, the microphone isn't sent (so Jarvis never
  hears itself through the speakers), but “Hey Jarvis” still interrupts it.
* After Jarvis answers, it keeps listening for a follow-up for a few seconds
  (longer while an action waits for you to say "confirm" or "cancel"), then
  hangs up and goes back to sleep.
* Muting ignores the microphone completely and hangs up any conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

import numpy as np
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from jarvis_satellite.config import Settings
from jarvis_satellite.pairing import TokenStore
from jarvis_satellite.protocol import (
    CLOSE_UNAUTHORIZED,
    INTERRUPT,
    explain_close,
    parse_event,
)
from jarvis_satellite.wake import WakeDetector

log = logging.getLogger("jarvis.satellite")
# The socket's own logger stays at INFO: at DEBUG, websockets logs request headers,
# and the device token is one of them.
_socket_log = logging.getLogger("jarvis.satellite.socket")
_socket_log.setLevel(logging.INFO)

READY_TIMEOUT = 5.0
SILENT_CALL_LIMIT = 60.0  # hang up if Jarvis says nothing at all for this long
MIC_RETRY_SECONDS = 3.0


class Mic(Protocol):
    def frames(self) -> AsyncIterator[np.ndarray]: ...


class Speaker(Protocol):
    @property
    def playing(self) -> bool: ...
    def play(self, pcm: bytes) -> None: ...
    def flush(self) -> None: ...
    def chime(self) -> None: ...


class Indicator(Protocol):
    def show(self, state: str, detail: str | None = None) -> None:
        """state: sleeping, connecting, listening, thinking, speaking, muted, unpaired, error."""
        ...


Connector = Callable[..., Any]  # websockets.asyncio.client.connect, or a stand-in


class Satellite:
    def __init__(
        self,
        settings: Settings,
        *,
        tokens: TokenStore,
        mic: Mic,
        speaker: Speaker,
        wake: WakeDetector,
        indicator: Indicator,
        connector: Connector = connect,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._tokens = tokens
        self._mic = mic
        self._speaker = speaker
        self._wake = wake
        self._indicator = indicator
        self._connector = connector
        self._clock = clock
        self.muted = False
        self._call: _Call | None = None

    def set_muted(self, muted: bool) -> None:
        """Mute or unmute (from the tray or the hotkey, via the event loop)."""
        self.muted = muted
        if muted and self._call is not None:
            self._call.hang_up()
        self._indicator.show("muted" if muted else "sleeping")

    def toggle_mute(self) -> None:
        self.set_muted(not self.muted)

    async def run(self) -> None:
        """Listen for the wake word until cancelled."""
        frames: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        pump = asyncio.create_task(self._pump(frames))
        self._indicator.show("muted" if self.muted else "sleeping")
        try:
            while True:
                frame = await frames.get()
                if self.muted:
                    continue
                if self._wake.detect(frame):
                    log.info("wake word heard")
                    await self._converse(frames)
                    self._wake.reset()
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    async def _pump(self, frames: asyncio.Queue[np.ndarray]) -> None:
        """Microphone to queue. A lost microphone (unplugged) is reopened."""
        while True:
            try:
                async for frame in self._mic.frames():
                    if frames.full():
                        frames.get_nowait()
                    frames.put_nowait(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("microphone problem: %s", exc)
                self._indicator.show("error", f"The microphone stopped ({exc}). Retrying…")
            await asyncio.sleep(MIC_RETRY_SECONDS)

    async def _converse(self, frames: asyncio.Queue[np.ndarray]) -> None:
        token = self._tokens.get(self.settings.server)
        if not token:
            self._indicator.show(
                "unpaired", "This PC isn't paired yet. Run: jarvis-satellite pair CODE"
            )
            return
        if self.settings.chime:
            self._speaker.chime()
        self._indicator.show("connecting")
        call = _Call(self, frames)
        self._call = call
        try:
            async with self._connector(
                self.settings.socket_url,
                additional_headers={"Authorization": f"Bearer {token}"},
                open_timeout=READY_TIMEOUT,
                max_size=2**20,
                logger=_socket_log,
            ) as socket:
                await call.run(socket)
        except (OSError, InvalidHandshake, InvalidURI, TimeoutError) as exc:
            log.warning("can't reach Jarvis: %s", type(exc).__name__)
            self._indicator.show(
                "error", f"Can't reach Jarvis at {self.settings.server}. Is it running?"
            )
            return
        finally:
            self._call = None
            self._speaker.flush()
        problem = explain_close(call.close_code, call.close_reason)
        if call.close_code == CLOSE_UNAUTHORIZED:
            self._indicator.show("unpaired", problem)
        elif problem:
            self._indicator.show("error", problem)
        elif not self.muted:
            self._indicator.show("sleeping")


class _Call:
    """One conversation, from the wake word until the follow-up window closes."""

    def __init__(self, satellite: Satellite, frames: asyncio.Queue[np.ndarray]) -> None:
        self._s = satellite
        self._frames = frames
        self._clock = satellite._clock
        self.ready = asyncio.Event()
        self.server_state = "idle"
        self.awaiting_confirmation = False
        self.last_activity = self._clock()
        self.close_code: int | None = None
        self.close_reason = ""
        self._hung_up = False

    def hang_up(self) -> None:
        self._hung_up = True

    async def run(self, socket: ClientConnection) -> None:
        receiver = asyncio.create_task(self._receive(socket))
        try:
            await self._talk(socket, receiver)
        finally:
            if not receiver.done():
                await socket.close()
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                await asyncio.wait_for(receiver, timeout=2)

    async def _talk(self, socket: ClientConnection, receiver: asyncio.Task[None]) -> None:
        s = self._s
        early: list[bytes] = []
        started = self._clock()
        # Keep what's said while connecting, and send it once Jarvis is ready.
        while not self.ready.is_set():
            if receiver.done() or self._hung_up or self._clock() - started > READY_TIMEOUT:
                return
            with contextlib.suppress(TimeoutError):
                frame = await asyncio.wait_for(self._frames.get(), timeout=0.1)
                s._wake.detect(frame)  # keeps the detector in step; a 2nd "Hey Jarvis" is ignored
                early.append(frame.tobytes())
        for chunk in early:
            await socket.send(chunk)
        self.last_activity = self._clock()
        while not (receiver.done() or self._hung_up):
            try:
                frame = await asyncio.wait_for(self._frames.get(), timeout=0.25)
            except TimeoutError:
                frame = None
            if frame is not None:
                action = self.decide(woke=s._wake.detect(frame))  # always fed: stays in step
                if action == "send":
                    await socket.send(frame.tobytes())
                elif action == "interrupt":  # "Hey Jarvis" over Jarvis: stop it and listen
                    s._speaker.flush()
                    await socket.send(INTERRUPT)
                    self.server_state = "listening"
                    self.last_activity = self._clock()
                    s._indicator.show("listening")
            if self._finished():
                return

    def decide(self, *, woke: bool) -> str:
        """What to do with a microphone frame: "send", "drop" or "interrupt".

        Half-duplex: while Jarvis is speaking, the microphone would hear Jarvis
        through the speakers, so it isn't sent; only the wake word gets through.
        """
        if self.server_state == "speaking" or self._s._speaker.playing:
            return "interrupt" if woke else "drop"
        return "send"

    def _finished(self) -> bool:
        quiet = self._clock() - self.last_activity
        if quiet > SILENT_CALL_LIMIT:
            return True
        if self.server_state != "idle" or self._s._speaker.playing:
            return False
        settings = self._s.settings
        if self.awaiting_confirmation:
            return quiet > settings.confirm_seconds
        return quiet > settings.follow_up_seconds

    async def _receive(self, socket: ClientConnection) -> None:
        s = self._s
        try:
            async for message in socket:
                self.last_activity = self._clock()
                if isinstance(message, bytes):
                    s._speaker.play(message)
                    continue
                event = parse_event(message)
                if event is None:
                    continue
                kind = event["type"]
                if kind == "ready":
                    self.ready.set()
                    s._indicator.show("listening")
                elif kind == "state":
                    self.server_state = str(event.get("state"))
                    shown = "listening" if self.server_state == "idle" else self.server_state
                    s._indicator.show(shown)
                elif kind == "interrupt":
                    s._speaker.flush()
                elif kind == "confirmation":
                    self.awaiting_confirmation = event.get("proposal_id") is not None
                elif kind == "error":
                    s._indicator.show("error", str(event.get("message", ""))[:200])
                # What you said and what Jarvis answered ("user", "assistant") is never
                # logged or kept here: it's in your conversation in the app.
        except ConnectionClosed:
            pass
        finally:
            self.close_code = socket.close_code
            self.close_reason = socket.close_reason or ""
