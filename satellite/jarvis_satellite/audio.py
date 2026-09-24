"""Microphone in, speaker out, at the rates Jarvis uses.

Devices are opened at their own sample rate (Windows' shared-mode audio won't
open a microphone at 16 kHz) and converted here: the microphone down to 16 kHz
with an anti-aliasing filter, Jarvis's 24 kHz voice up to the speaker's rate.
sounddevice is imported only when a device is opened, so everything else runs
(and is tested) without audio hardware.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from jarvis_satellite.protocol import FRAME_SAMPLES, INPUT_RATE, OUTPUT_RATE

log = logging.getLogger("jarvis.satellite")


def low_pass(taps: int, cutoff: float) -> np.ndarray:
    """A windowed-sinc low-pass filter; `cutoff` is a fraction of the sample rate."""
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
    return (h / h.sum()).astype(np.float64)


class Resampler:
    """Streaming sample-rate conversion; chunking doesn't change the result."""

    def __init__(self, source: int, target: int) -> None:
        self.source, self.target = source, target
        self._step = source / target
        self._filter = low_pass(31, 0.45 * target / source) if source > target else None
        self._history = np.zeros(30 if self._filter is not None else 0)
        self._position = 1.0  # where the next output falls, relative to [last, *chunk]
        self._last = 0.0

    def process(self, chunk: np.ndarray) -> np.ndarray:
        x = np.asarray(chunk, dtype=np.float64)
        if self.source == self.target:
            return x.astype(np.float32)
        if self._filter is not None:
            joined = np.concatenate([self._history, x])
            x = np.convolve(joined, self._filter, mode="valid")
            self._history = joined[len(joined) - len(self._history) :]
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        count = max(0, int(np.ceil((len(x) - self._position) / self._step)))
        positions = self._position + self._step * np.arange(count)
        padded = np.concatenate([[self._last], x])
        index = np.floor(positions).astype(np.int64)
        fraction = positions - index
        out = padded[index] + (padded[index + 1] - padded[index]) * fraction
        self._position = (positions[-1] + self._step if count else self._position) - len(x)
        self._last = float(x[-1])
        return out.astype(np.float32)


class Framer:
    """Collects samples into fixed-size frames."""

    def __init__(self, size: int = FRAME_SAMPLES) -> None:
        self.size = size
        self._buffer = np.zeros(0, dtype=np.float32)

    def push(self, samples: np.ndarray) -> list[np.ndarray]:
        self._buffer = np.concatenate([self._buffer, samples.astype(np.float32)])
        frames = []
        while len(self._buffer) >= self.size:
            frames.append(self._buffer[: self.size])
            self._buffer = self._buffer[self.size :]
        return frames


def to_int16(samples: np.ndarray) -> np.ndarray:
    return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)


def rms(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return 0.0
    x = samples.astype(np.float64)
    if samples.dtype == np.int16:
        x /= 32768
    return float(np.sqrt(np.mean(x * x)))


class Microphone:
    """The chosen (or default) microphone, as 80 ms frames of 16 kHz int16 audio."""

    def __init__(self, device: str = "") -> None:
        self.device = device or None

    async def frames(self) -> AsyncIterator[np.ndarray]:
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        info: Any = sd.query_devices(self.device, "input")
        rate = int(info["default_samplerate"])
        resampler = Resampler(rate, INPUT_RATE)
        framer = Framer(FRAME_SAMPLES)

        def deliver(frame: np.ndarray) -> None:
            if queue.full():
                queue.get_nowait()  # fall behind? drop the oldest, stay live
            queue.put_nowait(frame)

        def callback(indata: np.ndarray, frames: int, time: Any, status: Any) -> None:
            for frame in framer.push(resampler.process(indata[:, 0])):
                loop.call_soon_threadsafe(deliver, to_int16(frame))

        stream = sd.InputStream(
            device=self.device,
            samplerate=rate,
            channels=1,
            dtype="float32",
            blocksize=int(rate * 0.02),
            callback=callback,
        )
        with stream:
            log.info("microphone open: %s at %d Hz", info.get("name", "default"), rate)
            while True:
                yield await queue.get()


class Speaker:
    """Plays Jarvis's voice from a queue that can be emptied instantly."""

    def __init__(self, device: str = "", *, rate: int | None = None) -> None:
        self.device = device or None
        self.rate = rate or 48_000
        self._lock = threading.Lock()
        self._chunks: deque[np.ndarray] = deque()
        self._offset = 0
        self._playing = False
        self._odd = b""
        self._resampler = Resampler(OUTPUT_RATE, self.rate)
        self._stream: Any = None

    def start(self) -> None:
        import sounddevice as sd

        info: Any = sd.query_devices(self.device, "output")
        self.rate = int(info["default_samplerate"])
        self._resampler = Resampler(OUTPUT_RATE, self.rate)
        self._stream = sd.OutputStream(
            device=self.device,
            samplerate=self.rate,
            channels=1,
            dtype="float32",
            blocksize=int(self.rate * 0.02),
            callback=self._fill,
        )
        self._stream.start()
        log.info("speaker open: %s at %d Hz", info.get("name", "default"), self.rate)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._playing or bool(self._chunks)

    def play(self, pcm: bytes) -> None:
        """Queue 16-bit PCM at 24 kHz (as Jarvis sends it)."""
        data = self._odd + pcm
        self._odd = data[-1:] if len(data) % 2 else b""
        data = data[: len(data) - len(self._odd)]
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
        self._enqueue(self._resampler.process(samples))

    def chime(self) -> None:
        """A short rising two-note chime: “I'm listening”."""
        notes = []
        for hz in (660.0, 880.0):
            t = np.arange(int(self.rate * 0.07)) / self.rate
            envelope = np.sin(np.pi * t / t[-1]) ** 2
            notes.append(0.18 * envelope * np.sin(2 * np.pi * hz * t))
        self._enqueue(np.concatenate(notes).astype(np.float32))

    def flush(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._offset = 0
            self._playing = False
        self._odd = b""
        self._resampler = Resampler(OUTPUT_RATE, self.rate)

    def _enqueue(self, samples: np.ndarray) -> None:
        if len(samples):
            with self._lock:
                self._chunks.append(samples)

    def _fill(self, outdata: np.ndarray, frames: int, time: Any, status: Any) -> None:
        """The audio thread asks for the next `frames` samples."""
        out = outdata[:, 0]
        written = 0
        with self._lock:
            while written < frames and self._chunks:
                chunk = self._chunks[0]
                n = min(frames - written, len(chunk) - self._offset)
                out[written : written + n] = chunk[self._offset : self._offset + n]
                written += n
                self._offset += n
                if self._offset >= len(chunk):
                    self._chunks.popleft()
                    self._offset = 0
            self._playing = written > 0
        out[written:] = 0
