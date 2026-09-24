"""Voice parts the API needs at runtime, built once at startup."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from jarvis.config import Settings
from jarvis.services import Services
from jarvis.voice.config import VoiceConfig, VoiceConfigError, load_voice_config
from jarvis.voice.devices import DeviceService
from jarvis.voice.speech import SpeechClient
from jarvis.voice.textdata import ensure_punkt

log = logging.getLogger("jarvis.voice")


@dataclass
class VoiceRuntime:
    devices: DeviceService
    config: VoiceConfig | None = None
    speech: SpeechClient | None = None
    config_error: str | None = None
    text_data_problem: str | None = None
    max_sessions: int = 2
    active_sessions: int = 0

    @property
    def enabled(self) -> bool:
        return self.config is not None and self.speech is not None

    @property
    def problem(self) -> str | None:
        """Why live voice can't run right now, if it can't."""
        return self.config_error or self.text_data_problem

    async def prepare(self) -> None:
        """Make sure the sentence data live voice needs is there (downloads it once)."""
        if not self.enabled:
            return
        try:
            self.text_data_problem = await asyncio.to_thread(ensure_punkt)
        except Exception:  # never let this take Jarvis down; voice just says why it's off
            log.exception("voice: checking the sentence data failed")
            self.text_data_problem = "Voice couldn't check its sentence data. See the logs."
        if self.text_data_problem:
            log.error("voice: %s", self.text_data_problem)

    async def aclose(self) -> None:
        if self.speech is not None:
            await self.speech.aclose()


def build_voice_runtime(settings: Settings, services: Services) -> VoiceRuntime:
    """Load voice.yaml. A broken file disables voice (and says why) instead of Jarvis."""
    devices = DeviceService(clock=services.clock, audit=services.audit)
    try:
        config = load_voice_config(settings.voice_config_path)
    except VoiceConfigError as exc:
        log.error("voice is off: %s", exc)
        return VoiceRuntime(devices=devices, config_error=str(exc))
    key = settings.speech_api_key.get_secret_value() if settings.speech_api_key else None
    speech = SpeechClient(settings.speech_base_url, config.speech, api_key=key)
    return VoiceRuntime(devices=devices, config=config, speech=speech)
