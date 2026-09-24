"""Make a synthetic “room” recording for a quick benchmark smoke test (CI uses it).

Ten minutes of background noise with speech that isn't the wake word, mixed in
every 20 seconds. It proves the benchmark works end to end; it's no substitute
for an hour of your real room (see false_accepts.py).

    python bench/synthetic_room.py room.wav
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

RATE = 16_000
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def main(out: str, minutes: float = 10.0) -> None:
    rng = np.random.default_rng(7)
    total = int(minutes * 60 * RATE)
    # Brown-ish noise: a hum and hiss, like a fan or distant traffic.
    noise = np.cumsum(rng.normal(0, 1, total))
    noise -= np.convolve(noise, np.ones(4_000) / 4_000, mode="same")
    room = 0.02 * noise / (np.abs(noise).max() or 1)
    with wave.open(str(FIXTURES / "question.wav")) as w:
        speech = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2") / 32768
    for start in range(RATE * 5, total - len(speech), RATE * 20):
        room[start : start + len(speech)] += 0.8 * speech
    pcm = (np.clip(room, -1, 1) * 32767).astype("<i2")
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    print(f"Wrote {out}: {minutes:g} minutes")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "room.wav")
