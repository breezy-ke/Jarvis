"""Load `config/brief.yaml` (when and where the brief arrives) and `config/sources.yaml`."""

from __future__ import annotations

import re
from datetime import time
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class BriefConfigError(ValueError):
    pass


_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
Category = Literal["web", "ai", "news", "africa", "tools", "security"]


class Channels(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    app: bool = True
    push: bool = True
    telegram: bool = True
    inbox: bool = True
    audio: bool = True


class Sections(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    top: int = Field(default=5, ge=1, le=10)
    security: int = Field(default=5, ge=0, le=10)
    africa: int = Field(default=4, ge=0, le=8)
    quick: int = Field(default=5, ge=0, le=10)


class BriefConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    time: str = "07:00"  # when it arrives, in your timezone (policies.yaml)
    prepare_minutes: int = Field(default=20, ge=5, le=120)
    max_window_hours: int = Field(default=72, ge=12, le=168)
    channels: Channels = Channels()
    sections: Sections = Sections()
    audio_minutes: int = Field(default=4, ge=1, le=10)
    keep_days: int = Field(default=30, ge=7, le=365)

    @field_validator("time")
    @classmethod
    def _time(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f'the time looks like "07:00", not "{value}"')
        return value

    @property
    def clock_time(self) -> time:
        return time(int(self.time[:2]), int(self.time[3:]))


class _Source(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str = Field(min_length=1, max_length=80)
    category: Category
    weight: float = Field(default=1.0, gt=0, le=3)
    every_hours: int = Field(default=1, ge=1, le=24)
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError(f'ids are lowercase letters, digits, - and _ ("{value}")')
        return value


class FeedSource(_Source):
    kind: Literal["feed"]
    url: str

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError(f"a feed address starts with https:// ({value})")
        return value


class HNSource(_Source):
    kind: Literal["hn"]
    min_points: int = Field(default=150, ge=1)
    stories: int = Field(default=40, ge=5, le=100)


class GitHubRisingSource(_Source):
    kind: Literal["github_rising"]
    min_stars: int = Field(default=200, ge=1)
    days: int = Field(default=7, ge=1, le=30)


class AdvisorySource(_Source):
    kind: Literal["github_advisories"]
    severities: tuple[Literal["low", "medium", "high", "critical"], ...] = ("high", "critical")
    # Extra packages, on top of the ones your profile's stacks imply.
    packages: dict[Literal["npm", "composer", "pip", "pub", "go", "maven", "nuget"], list[str]] = (
        Field(default_factory=dict)
    )


class KEVSource(_Source):
    kind: Literal["cisa_kev"]
    url: str = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    # Only entries whose vendor or product mentions one of these.
    match: tuple[str, ...] = ()


Source = Annotated[
    FeedSource | HNSource | GitHubRisingSource | AdvisorySource | KEVSource,
    Field(discriminator="kind"),
]


class SourcesConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    sources: tuple[Source, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> SourcesConfig:
        seen: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                raise ValueError(f'two sources are called "{source.id}"')
            seen.add(source.id)
        return self

    @property
    def enabled(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]

    def get(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)


def _load(path: Path, what: str) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BriefConfigError(f"{what} not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise BriefConfigError(f"{path.name} isn't valid YAML: {exc}") from exc


def parse_brief_config(raw: object, *, source: str = "brief.yaml") -> BriefConfig:
    try:
        return BriefConfig.model_validate(raw if raw is not None else {})
    except ValidationError as exc:
        raise BriefConfigError(f"Invalid {source}:\n{exc}") from exc


def parse_sources_config(raw: object, *, source: str = "sources.yaml") -> SourcesConfig:
    try:
        return SourcesConfig.model_validate(raw if raw is not None else {})
    except ValidationError as exc:
        raise BriefConfigError(f"Invalid {source}:\n{exc}") from exc


def load_brief_config(path: Path) -> BriefConfig:
    return parse_brief_config(_load(path, "brief settings"), source=path.name)


def load_sources_config(path: Path) -> SourcesConfig:
    return parse_sources_config(_load(path, "brief sources"), source=path.name)
