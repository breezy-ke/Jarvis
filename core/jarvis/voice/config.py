"""Load `config/voice.yaml`: speech models, voice, and the voice-approval phrases."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Words people say casually or that speech recognition hears by mistake. On
# their own they must never approve anything.
TOO_EASY_TO_SAY = frozenset({"yes", "yeah", "yep", "ok", "okay", "sure", "right", "fine", "go"})


class VoiceConfigError(ValueError):
    pass


def normalize_phrase(text: str) -> str:
    """Lowercase, drop punctuation and extra spaces: how phrases are compared."""
    text = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    text = re.sub(r"[^\w\s']", " ", text)
    return " ".join(text.split())


class SpeechConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stt_model: str = Field(min_length=3)
    language: str = "auto"
    tts_model: str = Field(min_length=3)
    voice: str = Field(min_length=2)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        value = value.strip().lower()
        if value != "auto" and not re.fullmatch(r"[a-z]{2,3}", value):
            raise ValueError('language must be "auto" or a language code such as "en" or "sw"')
        return value

    @property
    def fixed_language(self) -> str | None:
        return None if self.language == "auto" else self.language


class ConversationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    filler_after_seconds: float = Field(default=1.5, ge=0.3, le=10)
    fillers: tuple[str, ...] = ("One moment.",)
    confirmation_seconds: int = Field(default=60, ge=10, le=600)


class VoiceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    speech: SpeechConfig
    conversation: ConversationConfig = ConversationConfig()
    confirm_phrases: tuple[str, ...] = Field(min_length=1)
    cancel_phrases: tuple[str, ...] = Field(min_length=1)

    @field_validator("confirm_phrases", "cancel_phrases")
    @classmethod
    def _normalize(cls, phrases: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(normalize_phrase(p) for p in phrases))
        if any(not p for p in normalized):
            raise ValueError("phrases can't be empty")
        return normalized

    @model_validator(mode="after")
    def _safe_phrases(self) -> VoiceConfig:
        risky = sorted(p for p in self.confirm_phrases if p in TOO_EASY_TO_SAY)
        if risky:
            raise ValueError(
                f"confirm_phrases can't be a single casual word ({', '.join(risky)}): "
                'use something deliberate like "confirm" or "go ahead"'
            )
        both = sorted(set(self.confirm_phrases) & set(self.cancel_phrases))
        if both:
            raise ValueError(f"phrases can't both confirm and cancel: {', '.join(both)}")
        return self


def parse_voice_config(raw: object, *, source: str = "voice.yaml") -> VoiceConfig:
    try:
        return VoiceConfig.model_validate(raw)
    except ValidationError as exc:
        raise VoiceConfigError(f"Invalid {source}:\n{exc}") from exc


def load_voice_config(path: Path) -> VoiceConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VoiceConfigError(f"voice settings not found: {path}") from exc
    return parse_voice_config(raw, source=path.name)
