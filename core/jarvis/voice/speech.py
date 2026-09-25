"""Client for the local speech server (speaches: faster-whisper + Kokoro).

It speaks the OpenAI audio API, but runs on your PC: audio never leaves it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from jarvis.voice.config import SpeechConfig

TTS_SAMPLE_RATE = 24_000  # Kokoro's native rate; PCM from the server is 16-bit mono


class SpeechError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpeechStatus:
    reachable: bool
    stt_ready: bool
    tts_ready: bool
    detail: str = ""
    key_refused: bool = False  # it's up, but won't take Jarvis's SPEECH_API_KEY


class SpeechClient:
    def __init__(
        self,
        base_url: str,
        config: SpeechConfig,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- Speech to text ---------------------------------------------------------

    async def transcribe(
        self, audio: bytes, *, filename: str = "audio.wav", prompt: str | None = None
    ) -> str:
        """Transcribe a complete recording (WAV, OGG/Opus, MP3...) to text."""
        data: dict[str, Any] = {"model": self.config.stt_model, "response_format": "json"}
        if self.config.fixed_language:
            data["language"] = self.config.fixed_language
        if prompt:
            data["prompt"] = prompt
        try:
            response = await self._http.post(
                "audio/transcriptions", data=data, files={"file": (filename, audio)}
            )
        except httpx.HTTPError as exc:
            raise SpeechError(f"speech server unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SpeechError(_explain(response, self.config.stt_model))
        return str(response.json().get("text", "")).strip()

    # --- Text to speech -----------------------------------------------------------

    def _speech_request(self, text: str, response_format: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.tts_model,
            "voice": self.config.voice,
            "input": text,
            "response_format": response_format,
            "speed": self.config.speed,
        }
        if response_format == "pcm":
            body["sample_rate"] = TTS_SAMPLE_RATE
        return body

    async def stream_pcm(self, text: str, *, chunk_size: int = 4_800) -> AsyncIterator[bytes]:
        """Speak `text`, yielding 16-bit mono PCM at 24 kHz as it's generated."""
        try:
            async with self._http.stream(
                "POST", "audio/speech", json=self._speech_request(text, "pcm")
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise SpeechError(_explain(response, self.config.tts_model))
                async for chunk in response.aiter_bytes(chunk_size):
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise SpeechError(f"speech server unreachable: {type(exc).__name__}") from exc

    async def synthesize(self, text: str, *, response_format: str = "mp3") -> bytes:
        """Speak `text` into a complete audio file (MP3 by default)."""
        try:
            response = await self._http.post(
                "audio/speech", json=self._speech_request(text, response_format)
            )
        except httpx.HTTPError as exc:
            raise SpeechError(f"speech server unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SpeechError(_explain(response, self.config.tts_model))
        return response.content

    # --- Models ---------------------------------------------------------------------

    async def downloaded_models(self) -> set[str]:
        response = await self._http.get("models", timeout=10)
        response.raise_for_status()
        return {str(m.get("id")) for m in response.json().get("data", [])}

    async def status(self) -> SpeechStatus:
        try:
            models = await self.downloaded_models()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                return SpeechStatus(
                    True, False, False, "it refused SPEECH_API_KEY", key_refused=True
                )
            return SpeechStatus(False, False, False, f"it answered with error {code}")
        except (httpx.HTTPError, ValueError) as exc:
            return SpeechStatus(False, False, False, f"unreachable ({type(exc).__name__})")
        stt, tts = self.config.stt_model in models, self.config.tts_model in models
        wanted = ((self.config.stt_model, stt), (self.config.tts_model, tts))
        missing = [model for model, ready in wanted if not ready]
        detail = "ready" if not missing else "not downloaded: " + ", ".join(missing)
        return SpeechStatus(True, stt, tts, detail)

    async def pull(self, model_id: str) -> str:
        """Ask the speech server to download a model (it keeps it for next time)."""
        response = await self._http.post(f"models/{model_id}", timeout=httpx.Timeout(1800.0))
        if response.status_code == 404:
            raise SpeechError(
                f"The speech server doesn't know the model '{model_id}'. "
                "Pick another in config/voice.yaml (make doctor lists some)."
            )
        if response.status_code >= 400:
            raise SpeechError(_explain(response, model_id))
        return response.text

    async def available_stt_models(self, *, limit: int = 8) -> list[str]:
        """A few speech-to-text models the server can download (for suggestions)."""
        try:
            response = await self._http.get(
                "registry", params={"task": "automatic-speech-recognition"}, timeout=20
            )
            response.raise_for_status()
            ids = [str(m.get("id")) for m in response.json().get("data", [])]
        except (httpx.HTTPError, ValueError):
            return []
        preferred = [i for i in ids if "turbo" in i or "large-v3" in i]
        return (preferred + [i for i in ids if i not in preferred])[:limit]


def _explain(response: httpx.Response, model: str) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    detail = str(detail)[:300]
    if response.status_code == 404 or "not found" in detail.lower():
        return f"The speech model '{model}' isn't downloaded yet: run `make pull-models`."
    if response.status_code in (401, 403):
        return "The speech server refused the request: check SPEECH_API_KEY in .env."
    return f"Speech server error {response.status_code}: {detail}"
