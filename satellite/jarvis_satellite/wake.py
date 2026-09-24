"""The wake word: “Hey Jarvis”, detected on this PC with openWakeWord.

Nothing is sent anywhere to decide whether you said it. The models (three small
ONNX files) are downloaded once, from openWakeWord's own release, and checked
against the SHA-256 sums pinned here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np

RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
WAKE_MODEL = "hey_jarvis_v0.1.onnx"
MODELS = {
    WAKE_MODEL: "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
    "melspectrogram.onnx": "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    "embedding_model.onnx": "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
}
FRAME_SECONDS = 0.08


class WakeError(Exception):
    pass


class WakeDetector(Protocol):
    def detect(self, frame: np.ndarray) -> bool:
        """Feed 80 ms of 16 kHz audio (int16). True when “Hey Jarvis” was just said."""
        ...

    def reset(self) -> None: ...


def models_ready(directory: Path) -> bool:
    return all(_valid(directory / name, digest) for name, digest in MODELS.items())


def _valid(path: Path, digest: str) -> bool:
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest


def fetch_models(directory: Path, *, client: httpx.Client | None = None) -> list[str]:
    """Download any missing or damaged model. Returns the names downloaded."""
    directory.mkdir(parents=True, exist_ok=True)
    http = client or httpx.Client(timeout=120, follow_redirects=True)
    fetched: list[str] = []
    try:
        for name, digest in MODELS.items():
            target = directory / name
            if _valid(target, digest):
                continue
            try:
                response = http.get(RELEASE + name)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise WakeError(f"Couldn't download {name} ({type(exc).__name__}).") from None
            got = hashlib.sha256(response.content).hexdigest()
            if got != digest:
                raise WakeError(f"{name} failed its checksum (got {got[:12]}…); not using it.")
            partial = target.with_suffix(".part")
            partial.write_bytes(response.content)
            partial.replace(target)
            fetched.append(name)
    finally:
        if client is None:
            http.close()
    return fetched


class OpenWakeWord:
    """openWakeWord's pretrained `hey_jarvis` model."""

    def __init__(self, models: Path, *, threshold: float = 0.5, refractory: float = 2.0) -> None:
        if not models_ready(models):
            raise WakeError("The wake-word models are missing. Run: jarvis-satellite fetch-models")
        try:
            from openwakeword.model import Model
        except ImportError:
            raise WakeError(
                "openWakeWord isn't installed. The satellite is built for Windows: "
                "install it with scripts\\windows\\install-satellite.ps1"
            ) from None
        self._model = Model(
            wakeword_models=[str(models / WAKE_MODEL)],
            melspec_model_path=str(models / "melspectrogram.onnx"),
            embedding_model_path=str(models / "embedding_model.onnx"),
            inference_framework="onnx",
        )
        self.threshold = threshold
        self._quiet_frames = round(refractory / FRAME_SECONDS)
        self._cooldown = 0
        self.last_score = 0.0

    def detect(self, frame: np.ndarray) -> bool:
        scores = self._model.predict(frame)  # always fed, so its audio history stays current
        self.last_score = float(max(scores.values(), default=0.0))
        if self._cooldown > 0:
            self._cooldown -= 1
            return False
        if self.last_score >= self.threshold:
            self._cooldown = self._quiet_frames
            self._model.reset()  # the same words mustn't trigger again
            return True
        return False

    def reset(self) -> None:
        self._model.reset()
        self._cooldown = 0
