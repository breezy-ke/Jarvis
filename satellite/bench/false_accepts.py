"""How often does “Hey Jarvis” wake by mistake? The target is fewer than 1 per hour.

Record an ordinary hour in the room where the PC is (TV, music, people talking,
just no “Hey Jarvis”), then score it:

    jarvis-satellite record --minutes 60 --out room.wav
    python bench/false_accepts.py room.wav
    python bench/false_accepts.py room.wav --positives my_hey_jarvis_clips/ --sweep

It reports false wakes per hour at your threshold (and, with --sweep, at others),
and with --positives, how often each threshold still catches you saying it.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

from jarvis_satellite.audio import Resampler
from jarvis_satellite.config import data_dir, load
from jarvis_satellite.protocol import FRAME_SAMPLES, INPUT_RATE
from jarvis_satellite.wake import FRAME_SECONDS, OpenWakeWord

TARGET_PER_HOUR = 1.0
REFRACTORY = 2.0  # seconds, as in the app
SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)


def read_wav(path: Path) -> np.ndarray:
    """16 kHz mono int16 audio from a WAV file (converted if needed)."""
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path}: expected 16-bit PCM")
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        channels, rate = w.getnchannels(), w.getframerate()
    audio = raw.reshape(-1, channels)[:, 0].astype(np.float32) / 32768
    if rate != INPUT_RATE:
        audio = Resampler(rate, INPUT_RATE).process(audio)
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


def scores(detector: OpenWakeWord, audio: np.ndarray) -> np.ndarray:
    """The wake-word score for every 80 ms frame."""
    detector.reset()
    model = detector._model  # raw scores, without the app's reset-on-wake
    out = []
    for start in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        out.append(max(model.predict(audio[start : start + FRAME_SAMPLES]).values()))
    return np.array(out)


def wakes(frame_scores: np.ndarray, threshold: float) -> int:
    """How many times the app would wake: crossings, at most one per refractory period."""
    quiet = round(REFRACTORY / FRAME_SECONDS)
    count, cooldown = 0, 0
    for score in frame_scores:
        if cooldown:
            cooldown -= 1
        elif score >= threshold:
            count += 1
            cooldown = quiet
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="How often does “Hey Jarvis” wake by mistake? (Target: under 1 per hour.)"
    )
    parser.add_argument("recording", type=Path, help="a WAV of the room with no wake word")
    parser.add_argument("--threshold", type=float, default=None, help="default: your setting")
    parser.add_argument("--positives", type=Path, help="a folder of “Hey Jarvis” WAV clips")
    parser.add_argument("--sweep", action="store_true", help="also try other thresholds")
    parser.add_argument("--models", type=Path, default=data_dir() / "models")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    threshold = args.threshold if args.threshold is not None else load().wake_threshold
    detector = OpenWakeWord(args.models, threshold=threshold)
    room = read_wav(args.recording)
    hours = len(room) / INPUT_RATE / 3600
    room_scores = scores(detector, room)
    silence = np.zeros(INPUT_RATE, dtype=np.int16)
    clip_scores = []
    if args.positives:
        for clip in sorted(args.positives.glob("*.wav")):
            padded = np.concatenate([silence, read_wav(clip), silence])
            clip_scores.append(scores(detector, padded))

    rows = []
    for t in sorted({threshold, *(SWEEP if args.sweep else ())}):
        false_wakes = wakes(room_scores, t)
        row: dict[str, float | int | None] = {
            "threshold": t,
            "false_wakes": false_wakes,
            "per_hour": round(false_wakes / hours, 2) if hours else None,
        }
        if clip_scores:
            caught = sum(1 for s in clip_scores if wakes(s, t) > 0)
            row["caught"] = round(caught / len(clip_scores), 3)
        rows.append(row)

    mine = next(r for r in rows if r["threshold"] == threshold)
    passed = mine["per_hour"] is not None and float(mine["per_hour"]) < TARGET_PER_HOUR
    if args.json:
        print(json.dumps({"hours": round(hours, 3), "rows": rows, "passed": passed}))
    else:
        print(f"Recording: {hours * 60:.1f} minutes")
        for row in rows:
            caught = f", catches {row['caught']:.0%} of clips" if "caught" in row else ""
            marker = "  <- your setting" if row["threshold"] == threshold else ""
            print(
                f"threshold {row['threshold']:.2f}: {row['false_wakes']} false wakes "
                f"({row['per_hour']}/hour){caught}{marker}"
            )
        verdict = "PASS" if passed else "FAIL"
        print(f"{verdict}: target is fewer than {TARGET_PER_HOUR:g} false wake per hour.")
        if hours < 0.9:
            print("(For a fair verdict, record at least an hour.)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
