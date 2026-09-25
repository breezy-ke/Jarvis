"""Load `config/policies.yaml` and enforce the rules that YAML cannot loosen."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jarvis.policy.types import Autonomy, Channel, Risk

NEVER_AUTONOMOUS_PATTERNS: tuple[str, ...] = (
    "payment.*",
    "money.*",
    "transfer.*",
    "*.delete",
    "deploy.production",
)
"""Action kinds that can never run at L3, whatever the YAML says."""

PASSKEY_ONLY_RISKS = frozenset({Risk.HIGH, Risk.CRITICAL})
_KIND_RE = re.compile(r"^[a-z][a-z_]*(\.[a-z][a-z_]*)+$")


class PolicyConfigError(ValueError):
    pass


class QuietHours(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: time
    end: time

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse(cls, value: object) -> object:
        if isinstance(value, str):
            return time.fromisoformat(value)
        return value

    def contains(self, moment: time) -> bool:
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end  # wraps past midnight

    def next_end(self, local_now: datetime) -> datetime:
        """The next moment quiet hours end, in local time."""
        candidate = local_now.replace(
            hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0
        )
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate


class Defaults(BaseModel):
    model_config = ConfigDict(frozen=True)

    undo_window_seconds: int = Field(default=60, ge=0, le=86_400)
    timezone: str = "Africa/Nairobi"
    quiet_hours: QuietHours | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class ActionKindPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str
    autonomy: Autonomy
    risk: Risk
    undo_window_seconds: int | None = Field(default=None, ge=0, le=86_400)
    validators: tuple[str, ...] = ()
    max_per_day: int | None = Field(default=None, ge=1)
    defer_during_quiet_hours: bool = False


class PoliciesConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    defaults: Defaults = Defaults()
    approval_channels: dict[Risk, tuple[Channel, ...]]
    action_kinds: dict[str, ActionKindPolicy]

    @model_validator(mode="after")
    def _enforce_invariants(self) -> PoliciesConfig:
        problems: list[str] = []
        for risk in Risk:
            channels = self.approval_channels.get(risk)
            if not channels:
                problems.append(f"approval_channels.{risk.value} must list at least one channel")
            elif risk in PASSKEY_ONLY_RISKS and set(channels) != {Channel.PWA_PASSKEY}:
                problems.append(
                    f"approval_channels.{risk.value} must be exactly [pwa_passkey]: "
                    f"{risk.value}-risk actions need a fresh passkey tap"
                )
        for kind, policy in self.action_kinds.items():
            if not _KIND_RE.match(kind):
                problems.append(f"action kind {kind!r} must look like 'area.verb'")
            if policy.autonomy == Autonomy.L3:
                if any(fnmatch(kind, pattern) for pattern in NEVER_AUTONOMOUS_PATTERNS):
                    problems.append(f"{kind}: money, deletions and production deploys cannot be L3")
                if policy.risk in PASSKEY_ONLY_RISKS:
                    problems.append(f"{kind}: {policy.risk.value}-risk actions cannot be L3")
            if len(set(policy.validators)) != len(policy.validators):
                problems.append(f"{kind}: validators are listed twice")
        if problems:
            raise PolicyConfigError("Unsafe policies.yaml:\n  - " + "\n  - ".join(problems))
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.defaults.timezone)

    def quiet_at(self, moment: datetime) -> bool:
        """Whether `moment` falls in your quiet hours, in your timezone."""
        quiet = self.defaults.quiet_hours
        return quiet is not None and quiet.contains(moment.astimezone(self.tz).time())

    def undo_window(self, kind: str) -> int:
        policy = self.action_kinds[kind]
        if policy.undo_window_seconds is not None:
            return policy.undo_window_seconds
        return self.defaults.undo_window_seconds


def parse_policies(raw: object, *, source: str = "policies.yaml") -> PoliciesConfig:
    try:
        return PoliciesConfig.model_validate(raw)
    except ValidationError as exc:
        raise PolicyConfigError(f"Invalid {source}:\n{exc}") from exc


def load_policies(path: Path) -> PoliciesConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyConfigError(f"policies file not found: {path}") from exc
    return parse_policies(raw, source=path.name)
