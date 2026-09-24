"""The owner's profile: the structured "who you are" that Jarvis works from.

Onboarding fills it in. It is versioned (every change is a new version),
editable on the "What Jarvis knows about me" page, and a compact summary
of it is part of every conversation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Identity(_Section):
    preferred_name: str | None = Field(
        default=None, description="How Jarvis should address you, e.g. a first name or 'boss'."
    )
    full_name: str | None = Field(default=None, description="Your full name, for documents.")
    pronouns: str | None = Field(default=None, description="Optional. Only if you want to share.")
    location: str | None = Field(
        default=None, description="City and country, e.g. 'Nairobi, Kenya'."
    )
    timezone: str | None = Field(default=None, description="IANA timezone, e.g. 'Africa/Nairobi'.")
    languages: list[str] = Field(default_factory=list, description="Languages you speak.")


class RateItem(_Section):
    service: str
    price: str = Field(description="Amount or range, e.g. '150000' or '120k-250k'.")
    currency: str = "KES"
    unit: str = Field(default="project", description="per project, per hour, per month...")
    notes: str | None = None


class Business(_Section):
    name: str | None = Field(default=None, description="Your consultancy's name.")
    tagline: str | None = None
    website: str | None = None
    services: list[str] = Field(default_factory=list, description="What you sell.")
    differentiators: list[str] = Field(
        default_factory=list, description="Why clients pick you over others."
    )
    rate_card: list[RateItem] = Field(default_factory=list)
    team: list[str] = Field(default_factory=list, description="Partners or staff, with roles.")


class IdealClient(_Section):
    play: str = Field(
        description="Which lead play: 'ea_smes', 'international', 'tenders' or 'agencies'."
    )
    description: str
    industries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    budget_range: str | None = None
    buying_signals: list[str] = Field(default_factory=list)


class Clients(_Section):
    ideal_clients: list[IdealClient] = Field(default_factory=list)
    current_clients: list[str] = Field(default_factory=list)
    never_pitch: list[str] = Field(
        default_factory=list, description="Companies or sectors you never want to approach."
    )


class CaseStudy(_Section):
    title: str
    client: str | None = None
    summary: str
    url: str | None = None
    stack: list[str] = Field(default_factory=list)


class Portfolio(_Section):
    portfolio_url: str | None = None
    highlights: list[CaseStudy] = Field(default_factory=list)


class Engineering(_Section):
    primary_stacks: list[str] = Field(
        default_factory=list, description="Stacks you build client work in."
    )
    also_uses: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list, description="Tools or stacks you avoid.")
    conventions: list[str] = Field(
        default_factory=list,
        description="House rules, e.g. 'TypeScript strict', 'pnpm', 'Conventional Commits'.",
    )
    hosting: list[str] = Field(default_factory=list, description="Where you deploy client work.")
    testing: list[str] = Field(default_factory=list)


class Design(_Section):
    style_keywords: list[str] = Field(
        default_factory=list, description="e.g. 'minimal', 'bold typography', 'warm'."
    )
    liked_references: list[str] = Field(
        default_factory=list, description="Sites/apps you admire, with what you like about them."
    )
    disliked_patterns: list[str] = Field(default_factory=list)
    brand_colors: list[str] = Field(default_factory=list)
    accessibility: str | None = Field(default=None, description="Your accessibility standard.")


class Communication(_Section):
    tone: str | None = Field(default=None, description="e.g. 'warm, direct, concise'.")
    formality: str | None = None
    greeting: str | None = Field(default=None, description="How you usually open emails.")
    sign_off: str | None = Field(default=None, description="How you usually close emails.")
    signature: str | None = None
    languages: list[str] = Field(default_factory=list)
    style_notes: list[str] = Field(default_factory=list)


class Schedule(_Section):
    working_hours: str | None = Field(default=None, description="e.g. 'Mon-Fri 08:00-18:00'.")
    focus_blocks: str | None = None
    routines: list[str] = Field(default_factory=list)
    do_not_disturb: str | None = None
    brief_time: str | None = Field(default=None, description="When to deliver the daily brief.")


class Goals(_Section):
    business_goals: list[str] = Field(default_factory=list)
    revenue_target: str | None = None
    learning_goals: list[str] = Field(default_factory=list)
    personal_goals: list[str] = Field(default_factory=list)


class Contact(_Section):
    name: str
    email: str | None = None
    relationship: str = Field(description="client, partner, family, friend, supplier...")
    vip: bool = False
    notes: str | None = None


class People(_Section):
    contacts: list[Contact] = Field(default_factory=list)


class Boundaries(_Section):
    never_do: list[str] = Field(default_factory=list, description="Things Jarvis must never do.")
    always_ask_before: list[str] = Field(default_factory=list)
    ok_without_asking: list[str] = Field(default_factory=list)
    sensitive_topics: list[str] = Field(default_factory=list)
    spending_limit: str | None = None


class Assistant(_Section):
    name: str = "Jarvis"
    personality: str | None = Field(
        default=None, description="e.g. 'dry British wit', 'straight to the point'."
    )
    voice: str | None = Field(default=None, description="Voice preset, e.g. 'bm_george'.")
    verbosity: str | None = None


class Personal(_Section):
    notes: list[str] = Field(default_factory=list)
    important_dates: list[str] = Field(default_factory=list)


class Profile(_Section):
    identity: Identity = Field(default_factory=Identity)
    business: Business = Field(default_factory=Business)
    clients: Clients = Field(default_factory=Clients)
    portfolio: Portfolio = Field(default_factory=Portfolio)
    engineering: Engineering = Field(default_factory=Engineering)
    design: Design = Field(default_factory=Design)
    communication: Communication = Field(default_factory=Communication)
    schedule: Schedule = Field(default_factory=Schedule)
    goals: Goals = Field(default_factory=Goals)
    people: People = Field(default_factory=People)
    boundaries: Boundaries = Field(default_factory=Boundaries)
    assistant: Assistant = Field(default_factory=Assistant)
    personal: Personal = Field(default_factory=Personal)


REQUIRED_FIELDS: tuple[str, ...] = (
    "identity.preferred_name",
    "identity.location",
    "identity.timezone",
    "business.name",
    "business.services",
    "business.rate_card",
    "clients.ideal_clients",
    "portfolio.highlights",
    "engineering.primary_stacks",
    "engineering.conventions",
    "design.style_keywords",
    "design.liked_references",
    "communication.tone",
    "communication.sign_off",
    "schedule.working_hours",
    "goals.business_goals",
    "people.contacts",
    "boundaries.always_ask_before",
    "boundaries.never_do",
    "assistant.personality",
)
"""Fields that must be filled before autonomy can switch on (80% threshold)."""

GATE_THRESHOLD = 0.8


class PathError(ValueError):
    pass


def section_fields() -> dict[str, list[str]]:
    return {name: list(field.annotation.model_fields) for name, field in _sections().items()}  # type: ignore[union-attr]


def _sections() -> dict[str, Any]:
    return dict(Profile.model_fields)


def get_path(data: dict[str, Any], path: str) -> Any:
    node: Any = data
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise PathError(f"unknown profile field {path!r}")
        node = node[key]
    return node


def set_path(data: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Return a copy of `data` with `path` set, validated against the schema."""
    parts = path.split(".")
    if len(parts) != 2 or parts[0] not in Profile.model_fields:
        raise PathError(f"unknown profile field {path!r}; use 'section.field'")
    section = Profile.model_fields[parts[0]].annotation
    if parts[1] not in section.model_fields:  # type: ignore[union-attr]
        raise PathError(f"unknown profile field {path!r}")
    updated = Profile.model_validate(data).model_dump(mode="json")
    updated[parts[0]][parts[1]] = value
    try:
        return Profile.model_validate(updated).model_dump(mode="json")
    except Exception as exc:
        raise PathError(f"invalid value for {path}: {exc}") from exc


def is_filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict):
        return len(value) > 0
    return True
