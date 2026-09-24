"""Load and validate `config/models.yaml`."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class PrivacyClass(StrEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    CONFIDENTIAL = "confidential"

    @property
    def rank(self) -> int:
        return _PRIVACY_RANK[self]

    def stricter(self, other: PrivacyClass | None) -> PrivacyClass:
        if other is None:
            return self
        return self if self.rank >= other.rank else other


_PRIVACY_RANK = {PrivacyClass.PUBLIC: 0, PrivacyClass.PERSONAL: 1, PrivacyClass.CONFIDENTIAL: 2}


class ProviderKind(StrEnum):
    OLLAMA = "ollama"
    GROQ = "groq"
    GOOGLE = "google"
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"
    FAKE = "fake"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProviderKind
    api_key_env: str | None = None
    base_url_env: str | None = None
    base_url: str | None = None
    local: bool = False
    paid: bool = False
    trains_on_data: bool
    zero_data_retention: bool = False


class Limits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rpm: int | None = Field(default=None, ge=1)
    rpd: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=1)
    tpd: int | None = Field(default=None, ge=1)


# Providers that accept the OpenAI-style `reasoning_effort` setting.
REASONING_KINDS = frozenset({"ollama", "openai", "openrouter", "openai_compatible", "fake"})


class ModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    limits: Limits = Limits()
    usd_per_mtok_in: float = Field(default=0.0, ge=0)
    usd_per_mtok_out: float = Field(default=0.0, ge=0)
    # "none" turns off a thinking model's reasoning pause (e.g. for voice).
    reasoning: Literal["none", "low", "medium", "high"] | None = None
    max_tokens: int | None = Field(default=None, ge=16, le=64_000)


class TaskConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    privacy: PrivacyClass
    candidates: tuple[str, ...] = Field(min_length=1)


class Budget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    monthly_usd_cap: float = Field(default=0.0, ge=0)
    alert_thresholds: tuple[float, ...] = (0.5, 0.8, 1.0)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    budget: Budget = Budget()
    quota_headroom: float = Field(default=0.9, gt=0, le=1)
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    tasks: dict[str, TaskConfig]

    @model_validator(mode="after")
    def _references(self) -> ModelsConfig:
        problems: list[str] = []
        for ref, model in self.models.items():
            if model.provider not in self.providers:
                problems.append(f"models.{ref}: unknown provider {model.provider!r}")
            elif (
                model.reasoning is not None
                and self.providers[model.provider].kind.value not in REASONING_KINDS
            ):
                problems.append(
                    f"models.{ref}: reasoning isn't supported for "
                    f"{self.providers[model.provider].kind.value} providers"
                )
        for name, task in self.tasks.items():
            for ref in task.candidates:
                if ref not in self.models:
                    problems.append(f"tasks.{name}: unknown model {ref!r}")
        if "chat" not in self.tasks:
            problems.append("tasks.chat is required")
        if problems:
            raise ValueError("Invalid models.yaml:\n  - " + "\n  - ".join(problems))
        return self


class ModelsConfigError(ValueError):
    pass


def parse_models_config(raw: object, *, source: str = "models.yaml") -> ModelsConfig:
    try:
        return ModelsConfig.model_validate(raw)
    except ValidationError as exc:
        raise ModelsConfigError(f"Invalid {source}:\n{exc}") from exc


def load_models_config(path: Path) -> ModelsConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ModelsConfigError(f"models file not found: {path}") from exc
    return parse_models_config(raw, source=path.name)
