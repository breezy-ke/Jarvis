"""The voice WebSocket protocol (`/api/voice/ws`), shared by the app and the satellite.

Client to Jarvis:
  * binary: your microphone, 16-bit little-endian mono PCM at 16 kHz
  * text:   JSON controls: {"type": "interrupt"} stops Jarvis speaking now

Jarvis to client:
  * binary: Jarvis's voice, 16-bit little-endian mono PCM at 24 kHz
  * text:   JSON events:
      {"type": "ready", "input_rate": 16000, "output_rate": 24000}
      {"type": "state", "state": "listening" | "thinking" | "speaking" | "idle"}
      {"type": "user", "text": "...", "final": true}           what Jarvis heard
      {"type": "assistant", "text": "...", "final": false}     reply text as it's spoken
      {"type": "filler", "text": "One moment."}
      {"type": "interrupt"}                                    stop playing queued audio
      {"type": "conversation", "id": "..."}                    open it in the app
      {"type": "confirmation", "proposal_id": "...", "summary": "..."}  (null when settled)
      {"type": "error", "message": "..."}
"""

from __future__ import annotations

import json
from typing import Any

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
MAX_MESSAGE_BYTES = 64 * 1024  # a JSON control message is tiny; audio chunks are ~1-4 KiB


class VoiceSerializer(FrameSerializer):
    """Raw PCM audio as binary frames; everything else as small JSON events."""

    def __init__(self) -> None:
        super().__init__()
        self._odd_byte = b""

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "interrupt"})
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            if self.should_ignore_frame(frame) or not isinstance(frame.message, dict):
                return None
            return json.dumps(frame.message, ensure_ascii=False)
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes):
            # 16-bit samples: carry a stray odd byte over to the next chunk.
            data = self._odd_byte + data
            if len(data) % 2:
                data, self._odd_byte = data[:-1], data[-1:]
            else:
                self._odd_byte = b""
            if not data:
                return None
            return InputAudioRawFrame(audio=data, sample_rate=INPUT_SAMPLE_RATE, num_channels=1)
        if len(data) > MAX_MESSAGE_BYTES:
            return None
        try:
            message: Any = json.loads(data)
        except ValueError:
            return None
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return None
        return InputTransportMessageFrame(message=message)
