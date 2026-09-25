"""How quickly does Jarvis answer out loud? (`make bench-voice`; the target is under 1.5 s.)

It asks a recorded question over the real voice socket, several times in one
conversation, the way a microphone would (in real time, then silence), each time
after Jarvis has finished answering. It measures from the moment the question's
last word was sent:

  heard     your words came back as text   (turn detection + speech-to-text)
  answered  the first words of the answer  (+ the language model)
  speaking  the first sound of the answer  (+ text-to-speech)   <- the target

A filler ("One moment.") isn't the answer, so its sound doesn't count. Every
turn must be answered for a pass. It uses a temporary device and its own
conversation (never mined for memories), and removes both after.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import statistics
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from jarvis.audit.log import AuditLog
from jarvis.clock import Clock
from jarvis.config import REPO_ROOT
from jarvis.db.models import BENCHMARK_CHANNEL, Conversation
from jarvis.db.session import SessionFactory, transaction
from jarvis.voice.devices import DeviceService

TARGET_SECONDS = 1.5
DEFAULT_AUDIO = REPO_ROOT / "core" / "tests" / "fixtures" / "audio" / "question.wav"
NAME = "Voice benchmark"
ACTOR = "benchmark"
CHUNK_BYTES = 640  # 20 ms at 16 kHz
READY_TIMEOUT = 10.0
TURN_TIMEOUT = 30.0
WIND_DOWN_SECONDS = 2.0  # after a turn cut short, before its conversation is removed


class BenchError(Exception):
    """The benchmark couldn't run; the message says why."""


@dataclass
class Turn:
    heard: float | None = None
    answered: float | None = None
    speaking: float | None = None
    filler: bool = False
    cut_off: bool = False  # Jarvis took the turn before the question was over
    finished: bool = False  # Jarvis finished answering and went quiet


@dataclass
class Report:
    asked: int  # turns planned; it stops early if one goes wrong
    turns: list[Turn] = field(default_factory=list)

    def _values(self, name: str) -> list[float]:
        return sorted(value for t in self.turns if (value := getattr(t, name)) is not None)

    def median(self, name: str) -> float | None:
        values = self._values(name)
        return statistics.median(values) if values else None

    def p90(self, name: str) -> float | None:
        values = self._values(name)
        return values[min(len(values) - 1, int(0.9 * len(values)))] if values else None

    @property
    def unanswered(self) -> int:
        return sum(1 for t in self.turns if t.speaking is None)

    @property
    def passed(self) -> bool:
        speaking = self.median("speaking")
        return (
            len(self.turns) == self.asked > 0
            and not self.unanswered
            and speaking is not None
            and speaking < TARGET_SECONDS
        )

    def render(self) -> str:
        count = f"{len(self.turns)}" + (
            f" of {self.asked}" if len(self.turns) != self.asked else ""
        )
        lines = [f"{count} turns; seconds from your last word:"]
        for name, meaning in (
            ("heard", "your words, as text"),
            ("answered", "first words of the answer"),
            ("speaking", "first sound of the answer"),
        ):
            median, p90 = self.median(name), self.p90(name)
            shown = (
                "no reply"
                if median is None or p90 is None
                else f"median {median:.2f}, p90 {p90:.2f}"
            )
            lines.append(f"  {name:<9} {shown}   ({meaning})")
        cut_off = sum(t.cut_off for t in self.turns)
        long = sum(1 for t in self.turns if t.speaking is not None and not t.finished)
        notes = (
            (cut_off, "Jarvis took the turn before the question was over"),
            (self.unanswered - cut_off, f"no spoken answer within {TURN_TIMEOUT:g} s"),
            (long, f"still answering after {TURN_TIMEOUT:g} s"),
            (sum(t.filler for t in self.turns), "a filler played while the answer started"),
        )
        lines += [f"  ({count} turn(s): {note})" for count, note in notes if count]
        verdict = "PASS" if self.passed else "FAIL"
        lines.append(
            f"{verdict}: the target is every turn answered, "
            f"with a median under {TARGET_SECONDS:g} s to speaking."
        )
        return "\n".join(lines)


def read_question(path: Path) -> tuple[bytes, int]:
    """16 kHz mono PCM, and the byte offset where the last word ends."""
    try:
        with wave.open(str(path)) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16_000, 1, 2):
                raise BenchError(f"{path} must be a 16 kHz mono 16-bit WAV file.")
            pcm = w.readframes(w.getnframes())
    except (OSError, wave.Error, EOFError) as exc:
        raise BenchError(f"Can't read {path} as a WAV file ({exc}).") from exc
    samples = memoryview(pcm).cast("h")
    last = max((i for i, s in enumerate(samples) if abs(s) > 500), default=len(samples) - 1)
    return pcm, (last + 1) * 2


async def _speak(ws: ClientConnection, pcm: bytes, speech_end: int, ended: list[float]) -> None:
    """Send the question in real time, then silence, like a microphone."""
    silence = b"\x00" * CHUNK_BYTES
    for offset in range(0, len(pcm), CHUNK_BYTES):
        await ws.send(pcm[offset : offset + CHUNK_BYTES])
        if not ended and offset + CHUNK_BYTES >= speech_end:
            ended.append(time.perf_counter())
        await asyncio.sleep(0.02)
    while True:
        await ws.send(silence)
        await asyncio.sleep(0.02)


def _closed(exc: ConnectionClosed) -> BenchError:
    code = exc.rcvd.code if exc.rcvd else None
    reason = (exc.rcvd.reason if exc.rcvd else "") or "no reason given"
    return BenchError(f"Jarvis closed the voice socket ({code}: {reason}).")


async def _wait_until_ready(ws: ClientConnection) -> None:
    try:
        async with asyncio.timeout(READY_TIMEOUT):
            while True:
                message = await ws.recv()
                if isinstance(message, str) and json.loads(message).get("type") == "ready":
                    return
    except ConnectionClosed as exc:
        raise _closed(exc) from exc
    except TimeoutError as exc:
        raise BenchError(f"Jarvis wasn't ready to listen within {READY_TIMEOUT:g} s.") from exc


async def ask(
    url: str, token: str, conversation_id: uuid.UUID, question: Path, report: Report
) -> None:
    """Ask the question over one socket, adding each turn to the report as it starts.

    It stops early if a turn goes wrong: asking again would talk over Jarvis.
    """
    pcm, speech_end = read_question(question)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with connect(
            f"{url}?conversation={conversation_id}", additional_headers=headers, max_size=2**22
        ) as ws:
            await _wait_until_ready(ws)
            for _ in range(report.asked):
                turn = Turn()
                report.turns.append(turn)
                await _one_turn(ws, turn, pcm, speech_end)
                if not turn.finished:
                    break
    except ConnectionClosed as exc:
        raise _closed(exc) from exc
    except (OSError, InvalidHandshake, InvalidURI) as exc:
        raise BenchError(
            f"Can't open the voice socket at {url} ({type(exc).__name__}). "
            "Is Jarvis running? (make up)"
        ) from exc


async def _one_turn(ws: ClientConnection, turn: Turn, pcm: bytes, speech_end: int) -> None:
    ended: list[float] = []
    speaker = asyncio.create_task(_speak(ws, pcm, speech_end, ended))
    try:
        async with asyncio.timeout(TURN_TIMEOUT):
            await _listen(ws, turn, ended)
    except TimeoutError:
        pass  # reported: no spoken answer, or not finished
    finally:
        speaker.cancel()
        with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
            await speaker


async def _listen(ws: ClientConnection, turn: Turn, ended: list[float]) -> None:
    answered_all = False  # all of the answer's text has arrived
    async for message in ws:
        now = time.perf_counter()
        if not ended:  # still asking
            if isinstance(message, str) and json.loads(message).get("type") == "user":
                turn.cut_off = True
                return
            continue
        since = now - ended[0]
        if isinstance(message, bytes):
            # The answer's first sound. A filler's sound comes before the answer's
            # text; the answer's own sound can only come after it.
            filler_sound = turn.filler and turn.answered is None
            if turn.heard is not None and turn.speaking is None and not filler_sound:
                turn.speaking = since
            continue
        event = json.loads(message)
        kind = event.get("type")
        if kind == "user" and turn.heard is None:
            turn.heard = since
        elif kind == "filler":
            turn.filler = True
        elif kind == "assistant":
            if event.get("text") and turn.answered is None:
                turn.answered = since
            if event.get("final") and not event.get("text"):
                answered_all = True
        elif kind == "state" and event.get("state") == "idle" and answered_all:
            turn.finished = True  # it has said all of it
            return


async def run_benchmark(
    session_factory: SessionFactory,
    *,
    clock: Clock,
    url: str,
    runs: int = 5,
    question: Path = DEFAULT_AUDIO,
) -> Report:
    if runs < 1:
        raise BenchError("Ask at least once (--runs 1 or more).")
    read_question(question)  # a bad file fails before anything is created
    devices = DeviceService(clock=clock, audit=AuditLog(clock))
    now = clock.now()
    conversation_id = uuid.uuid4()
    async with transaction(session_factory) as session:
        device, token = await devices.create_device(session, NAME, actor=ACTOR)
        session.add(
            Conversation(
                id=conversation_id,
                title=NAME,
                channel=BENCHMARK_CHANNEL,
                created_at=now,
                updated_at=now,
            )
        )
    report = Report(asked=runs)
    try:
        await ask(url, token, conversation_id, question, report)
    finally:
        if report.turns and not report.turns[-1].finished:
            await asyncio.sleep(WIND_DOWN_SECONDS)  # let Jarvis finish saving that turn
        async with transaction(session_factory) as session:
            await devices.revoke(session, device.id, actor=ACTOR)
            row = await session.get(Conversation, conversation_id)
            if row is not None:
                await session.delete(row)  # its messages go with it
    return report
