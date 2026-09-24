"""Jarvis's voice socket, as the satellite sees it (core/jarvis/voice/protocol.py).

Up: 16-bit mono PCM at 16 kHz (binary) and {"type": "interrupt"} (JSON).
Down: 16-bit mono PCM at 24 kHz (binary) and small JSON events.
"""

from __future__ import annotations

import json
from typing import Any

INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
FRAME_SAMPLES = 1_280  # 80 ms at 16 kHz: what the wake-word model expects

CLOSE_UNAUTHORIZED = 4401
CLOSE_BUSY = 4429
CLOSE_UNAVAILABLE = 4503

INTERRUPT = json.dumps({"type": "interrupt"})


def parse_event(message: str) -> dict[str, Any] | None:
    """A JSON event from Jarvis, or None if it isn't one."""
    try:
        event = json.loads(message)
    except ValueError:
        return None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return None
    return event


def explain_close(code: int | None, reason: str) -> str | None:
    """What to tell the owner when Jarvis closes the socket, or None if it's normal."""
    if code in (None, 1000, 1001):
        return None
    if code == CLOSE_UNAUTHORIZED:
        return "This PC isn't paired any more. Pair it again from Jarvis → Settings → Voice."
    if code == CLOSE_BUSY:
        return "Voice is already open somewhere else (the app or another PC)."
    if code == CLOSE_UNAVAILABLE:
        return reason or "Jarvis's voice isn't available right now. Run `make doctor` on the PC."
    return reason or f"The connection to Jarvis closed ({code})."
