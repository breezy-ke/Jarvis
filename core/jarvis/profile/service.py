"""Profile versions, completeness, sign-off, and the autonomy gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.log import AuditLog
from jarvis.clock import Clock
from jarvis.db.models import ProfileVersion
from jarvis.policy.state import GateStatus
from jarvis.profile.schema import (
    GATE_THRESHOLD,
    REQUIRED_FIELDS,
    PathError,
    Profile,
    get_path,
    is_filled,
    set_path,
)

_PROFILE_LOCK_KEY = 0x50524F46  # "PROF"


@dataclass(frozen=True)
class Completeness:
    score: float
    filled: list[str]
    missing: list[str]

    @property
    def meets_gate(self) -> bool:
        return self.score >= GATE_THRESHOLD


@dataclass(frozen=True)
class ProfileSnapshot:
    version: int
    profile: Profile
    signed_off_at: datetime | None
    ever_signed_off: bool

    @property
    def data(self) -> dict[str, Any]:
        return self.profile.model_dump(mode="json")


class SignOffError(ValueError):
    pass


def completeness(profile: Profile) -> Completeness:
    data = profile.model_dump(mode="json")
    filled = [p for p in REQUIRED_FIELDS if is_filled(get_path(data, p))]
    missing = [p for p in REQUIRED_FIELDS if p not in filled]
    return Completeness(score=len(filled) / len(REQUIRED_FIELDS), filled=filled, missing=missing)


class ProfileService:
    def __init__(self, *, audit: AuditLog, clock: Clock) -> None:
        self._audit = audit
        self._clock = clock

    async def current(self, session: AsyncSession) -> ProfileSnapshot:
        row = await session.scalar(
            select(ProfileVersion).order_by(ProfileVersion.version.desc()).limit(1)
        )
        signed = await session.scalar(
            select(func.count())
            .select_from(ProfileVersion)
            .where(ProfileVersion.signed_off_at.is_not(None))
        )
        if row is None:
            return ProfileSnapshot(0, Profile(), None, False)
        return ProfileSnapshot(
            version=row.version,
            profile=Profile.model_validate(row.data),
            signed_off_at=row.signed_off_at,
            ever_signed_off=bool(signed),
        )

    async def update(
        self,
        session: AsyncSession,
        changes: dict[str, Any],
        *,
        created_by: str,
        note: str = "",
    ) -> ProfileSnapshot:
        """Apply `{"section.field": value}` changes as one new version."""
        if not changes:
            raise PathError("no changes given")
        # Serialise writers so two updates can never both become version N+1.
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _PROFILE_LOCK_KEY})
        current = await self.current(session)
        data = current.data
        for path, value in changes.items():
            data = set_path(data, path, value)
        if data == current.data:
            return current
        version = current.version + 1
        session.add(
            ProfileVersion(
                version=version,
                data=data,
                created_at=self._clock.now(),
                created_by=created_by,
                note=note,
            )
        )
        await session.flush()
        await self._audit.append(
            session,
            actor=created_by,
            event_type="profile.updated",
            subject_type="profile",
            subject_id=str(version),
            summary=f"Profile updated: {', '.join(sorted(changes))}",
            data={"fields": sorted(changes), "version": version},
        )
        return ProfileSnapshot(version, Profile.model_validate(data), None, current.ever_signed_off)

    async def sign_off(self, session: AsyncSession, *, actor: str = "owner") -> ProfileSnapshot:
        current = await self.current(session)
        result = completeness(current.profile)
        if not result.meets_gate:
            raise SignOffError(
                f"Your profile is {result.score:.0%} complete; at least "
                f"{GATE_THRESHOLD:.0%} is needed before you can sign it off."
            )
        row = await session.get(ProfileVersion, current.version, with_for_update=True)
        assert row is not None
        row.signed_off_at = self._clock.now()
        await self._audit.append(
            session,
            actor=actor,
            event_type="profile.signed_off",
            subject_type="profile",
            subject_id=str(current.version),
            summary=f"Profile v{current.version} signed off ({result.score:.0%} complete)",
            data={"version": current.version, "score": round(result.score, 3)},
        )
        return ProfileSnapshot(current.version, current.profile, row.signed_off_at, True)

    async def history(self, session: AsyncSession, *, limit: int = 50) -> list[ProfileVersion]:
        rows = await session.scalars(
            select(ProfileVersion).order_by(ProfileVersion.version.desc()).limit(limit)
        )
        return list(rows)

    # --- AutonomyGate protocol ---------------------------------------------------------

    async def status(self, session: AsyncSession) -> GateStatus:
        snapshot = await self.current(session)
        result = completeness(snapshot.profile)
        if not result.meets_gate:
            return GateStatus(
                open=False,
                reason=f"your profile is {result.score:.0%} complete "
                f"(needs {GATE_THRESHOLD:.0%}) and not yet signed off",
            )
        if not snapshot.ever_signed_off:
            return GateStatus(open=False, reason="your profile is complete but not yet signed off")
        return GateStatus(open=True, reason="profile signed off")

    # --- Known contacts (for the recipients_known validator) -----------------------------

    async def is_known(self, session: AsyncSession, address: str) -> bool:
        snapshot = await self.current(session)
        known = {c.email.strip().lower() for c in snapshot.profile.people.contacts if c.email}
        return address.strip().lower() in known


def core_summary(profile: Profile, *, max_chars: int = 3_500) -> str:
    """A compact profile summary for every system prompt. Empty fields are omitted."""
    data = profile.model_dump(mode="json", exclude_defaults=True)
    lines: list[str] = []

    def render(value: Any) -> str:
        if isinstance(value, list):
            return "; ".join(render(v) for v in value)
        if isinstance(value, dict):
            return ", ".join(f"{k}: {render(v)}" for k, v in value.items() if is_filled(v))
        return str(value)

    for section, fields in data.items():
        if not isinstance(fields, dict):
            continue
        parts = [f"{k}: {render(v)}" for k, v in fields.items() if is_filled(v)]
        if parts:
            lines.append(f"- **{section}** — " + " | ".join(parts))
    if not lines:
        return "(The owner's profile is still empty: onboarding hasn't happened yet.)"
    summary = "\n".join(lines)
    if len(summary) > max_chars:
        summary = summary[:max_chars].rstrip() + " …"
    return summary
