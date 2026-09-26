"""Load `config/email.yaml`: sync timing, auto-drafts, alerts and digests."""

from __future__ import annotations

import re
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class EmailConfigError(ValueError):
    pass


class SyncConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_seconds: int = Field(default=60, ge=15, le=3600)
    first_sync_days: int = Field(default=14, ge=1, le=90)
    body_retention_days: int = Field(default=90, ge=7, le=3650)


class DraftingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    auto_draft: Literal["known", "all", "off"] = "known"


class AlertsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    urgent: Literal["known", "all", "off"] = "known"
    max_per_hour: int = Field(default=6, ge=1, le=60)


class DigestsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    times: tuple[str, ...] = ("07:15", "17:30")

    @field_validator("times")
    @classmethod
    def _times(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", item):
                raise ValueError(f'digest times look like "07:15", not "{item}"')
        if len(set(value)) != len(value):
            raise ValueError("digest times must be different")
        return tuple(sorted(value))

    @property
    def clock_times(self) -> list[time]:
        return [time(int(t[:2]), int(t[3:])) for t in self.times]


class EmailConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    sync: SyncConfig = SyncConfig()
    drafting: DraftingConfig = DraftingConfig()
    alerts: AlertsConfig = AlertsConfig()
    digests: DigestsConfig = DigestsConfig()


def parse_email_config(raw: object, *, source: str = "email.yaml") -> EmailConfig:
    try:
        return EmailConfig.model_validate(raw if raw is not None else {})
    except ValidationError as exc:
        raise EmailConfigError(f"Invalid {source}:\n{exc}") from exc


def load_email_config(path: Path) -> EmailConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EmailConfigError(f"email settings not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise EmailConfigError(f"{path.name} isn't valid YAML: {exc}") from exc
    return parse_email_config(raw, source=path.name)
