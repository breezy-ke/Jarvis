from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from jarvis.audit.log import AuditLog
from jarvis.clock import FrozenClock
from jarvis.db.models import AuditEvent, Fact
from jarvis.db.session import SessionFactory, transaction
from jarvis.memory.embeddings import HashEmbedder
from jarvis.memory.retrieval import render_facts, search_episodes, search_facts
from jarvis.memory.store import FactInput, FactStatus, MemoryStore, MemoryStoreError
from jarvis.profile.schema import REQUIRED_FIELDS, PathError, Profile
from jarvis.profile.service import ProfileService, SignOffError, completeness, core_summary

pytestmark = pytest.mark.db

FULL_PROFILE: dict[str, Any] = {
    "identity.preferred_name": "Brian",
    "identity.location": "Nairobi, Kenya",
    "identity.timezone": "Africa/Nairobi",
    "business.name": "Breezy Digital",
    "business.services": ["Web design", "Web apps"],
    "business.rate_card": [{"service": "Landing page", "price": "80000", "currency": "KES"}],
    "clients.ideal_clients": [
        {"play": "ea_smes", "description": "Nairobi SMEs with outdated sites"}
    ],
    "portfolio.highlights": [{"title": "Clinic site", "summary": "Booking + SEO"}],
    "engineering.primary_stacks": ["Next.js", "Laravel"],
    "engineering.conventions": ["TypeScript strict"],
    "design.style_keywords": ["minimal"],
    "design.liked_references": ["linear.app: calm typography"],
    "communication.tone": "warm and concise",
    "communication.sign_off": "Best regards",
    "schedule.working_hours": "Mon-Fri 08:00-18:00",
    "goals.business_goals": ["3 new retainers this quarter"],
    "people.contacts": [
        {"name": "Achieng", "email": "Achieng@Client.co.ke", "relationship": "client", "vip": True}
    ],
    "boundaries.always_ask_before": ["sending any email"],
    "boundaries.never_do": ["pay anyone"],
    "assistant.personality": "dry British wit",
}


@pytest.fixture
def profiles(audit: AuditLog, clock: FrozenClock) -> ProfileService:
    return ProfileService(audit=audit, clock=clock)


@pytest.fixture
def memory(audit: AuditLog, clock: FrozenClock) -> MemoryStore:
    return MemoryStore(embedder=HashEmbedder(), audit=audit, clock=clock)


# --- Profile -----------------------------------------------------------------------------


async def test_profile_starts_empty(
    session_factory: SessionFactory, profiles: ProfileService
) -> None:
    async with session_factory() as session:
        snapshot = await profiles.current(session)
        gate = await profiles.status(session)
    assert snapshot.version == 0
    assert completeness(snapshot.profile).score == 0
    assert not gate.open
    assert "0% complete" in gate.reason


async def test_updates_create_versions(
    session_factory: SessionFactory, profiles: ProfileService
) -> None:
    async with transaction(session_factory) as session:
        v1 = await profiles.update(
            session, {"identity.preferred_name": "Brian"}, created_by="owner"
        )
    async with transaction(session_factory) as session:
        v2 = await profiles.update(session, {"business.name": "Breezy"}, created_by="owner")
    async with transaction(session_factory) as session:
        same = await profiles.update(session, {"business.name": "Breezy"}, created_by="owner")
    assert (v1.version, v2.version, same.version) == (1, 2, 2)  # no-op writes no version
    assert v2.profile.identity.preferred_name == "Brian"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("identity.nope", "x"),
        ("nope.field", "x"),
        ("identity", "x"),
        ("business.rate_card", [{"price": "10"}]),  # missing required 'service'
        ("people.contacts", "not-a-list"),
    ],
)
async def test_invalid_updates_are_refused(
    session_factory: SessionFactory, profiles: ProfileService, path: str, value: object
) -> None:
    async with transaction(session_factory) as session:
        with pytest.raises(PathError):
            await profiles.update(session, {path: value}, created_by="owner")


async def test_concurrent_updates_never_collide(
    session_factory: SessionFactory, profiles: ProfileService
) -> None:
    async def write(i: int) -> None:
        async with transaction(session_factory) as session:
            await profiles.update(session, {"personal.notes": [f"note {i}"]}, created_by="t")

    await asyncio.gather(*(write(i) for i in range(8)))
    async with session_factory() as session:
        assert (await profiles.current(session)).version == 8


async def test_sign_off_opens_the_gate(
    session_factory: SessionFactory, profiles: ProfileService
) -> None:
    async with transaction(session_factory) as session:
        await profiles.update(session, {"identity.preferred_name": "Brian"}, created_by="owner")
        with pytest.raises(SignOffError, match="at least 80%"):
            await profiles.sign_off(session)
    async with transaction(session_factory) as session:
        await profiles.update(session, FULL_PROFILE, created_by="owner")
        assert not (await profiles.status(session)).open  # complete but not signed off
        await profiles.sign_off(session)
    async with session_factory() as session:
        gate = await profiles.status(session)
    assert gate.open


def test_required_fields_exist_in_schema() -> None:
    data = Profile().model_dump()
    for path in REQUIRED_FIELDS:
        section, field = path.split(".")
        assert field in data[section], path


async def test_known_contacts_are_case_insensitive(
    session_factory: SessionFactory, profiles: ProfileService
) -> None:
    async with transaction(session_factory) as session:
        await profiles.update(session, FULL_PROFILE, created_by="owner")
    async with session_factory() as session:
        assert await profiles.is_known(session, "achieng@client.co.ke")
        assert not await profiles.is_known(session, "stranger@client.co.ke")


def test_core_summary_skips_empty_fields() -> None:
    assert "still empty" in core_summary(Profile())
    profile = Profile.model_validate({"identity": {"preferred_name": "Brian"}})
    summary = core_summary(profile)
    assert "preferred_name: Brian" in summary
    assert "business" not in summary


# --- Memory ------------------------------------------------------------------------------


def fact(
    value: str, *, predicate: str = "prefers stack", status: FactStatus = FactStatus.INFERRED
) -> FactInput:
    return FactInput(
        category="preference",
        subject="owner",
        predicate=predicate,
        value=value,
        source="test",
        status=status,
    )


async def test_new_values_supersede_old_ones(
    session_factory: SessionFactory, memory: MemoryStore
) -> None:
    async with transaction(session_factory) as session:
        first = await memory.add(session, fact("Laravel"), actor="t")
    async with transaction(session_factory) as session:
        second = await memory.add(session, fact("Next.js"), actor="t")
    async with session_factory() as session:
        old = await session.get(Fact, first.id)
        assert old is not None
        assert old.valid_to is not None
        assert old.status == FactStatus.SUPERSEDED
        current = await memory.list(session)
    assert [f.id for f in current] == [second.id]


async def test_guesses_never_override_confirmed_facts(
    session_factory: SessionFactory, memory: MemoryStore
) -> None:
    async with transaction(session_factory) as session:
        confirmed = await memory.add(
            session, fact("Next.js", status=FactStatus.CONFIRMED), actor="t"
        )
        guess = await memory.add(session, fact("Django"), actor="t")
    assert guess.predicate.endswith("(suggested update)")
    async with session_factory() as session:
        still = await session.get(Fact, confirmed.id)
    assert still is not None
    assert still.valid_to is None


async def test_repeating_a_fact_reinforces_it(
    session_factory: SessionFactory, memory: MemoryStore
) -> None:
    async with transaction(session_factory) as session:
        a = await memory.add(session, fact("Next.js"), actor="t")
        b = await memory.add(session, fact("next.js", status=FactStatus.CONFIRMED), actor="t")
    assert a.id == b.id
    assert b.status == FactStatus.CONFIRMED


async def test_confirm_reject_edit(session_factory: SessionFactory, memory: MemoryStore) -> None:
    async with transaction(session_factory) as session:
        f = await memory.add(session, fact("Vue"), actor="t")
        await memory.confirm(session, f.id)
        edited = await memory.edit(session, f.id, "Nuxt")
        assert edited.status == FactStatus.CONFIRMED
        await memory.reject(session, edited.id)
        with pytest.raises(MemoryStoreError):
            await memory.confirm(session, edited.id)


async def test_forget_soft_then_purge(
    session_factory: SessionFactory, memory: MemoryStore, clock: FrozenClock
) -> None:
    async with transaction(session_factory) as session:
        f = await memory.add(session, fact("Svelte"), actor="t")
        await memory.forget(session, f.id, hard=False)
    async with session_factory() as session:
        hits = await search_facts(session, memory.embedder, "svelte", now=clock.now())
    assert hits == []
    clock.advance(days=8)
    async with transaction(session_factory) as session:
        assert await memory.purge_forgotten(session, older_than=timedelta(days=7)) == 1
    async with session_factory() as session:
        assert await session.get(Fact, f.id) is None


async def test_hard_delete_is_immediate(
    session_factory: SessionFactory, memory: MemoryStore
) -> None:
    async with transaction(session_factory) as session:
        f = await memory.add(session, fact("Angular"), actor="t")
        await memory.forget(session, f.id, hard=True)
    async with session_factory() as session:
        assert await session.get(Fact, f.id) is None


async def test_audit_never_contains_memory_content(
    session_factory: SessionFactory, memory: MemoryStore
) -> None:
    secret = "my daughter's school is Riverside Primary"
    async with transaction(session_factory) as session:
        f = await memory.add(
            session,
            FactInput("identity", "owner", "family detail", secret, source="test"),
            actor="t",
        )
        await memory.forget(session, f.id, hard=True)
    async with session_factory() as session:
        dumps = [e.canonical for e in await session.scalars(select(AuditEvent))]
    assert dumps
    assert all("Riverside" not in d for d in dumps)


async def test_hybrid_search_finds_the_relevant_fact(
    session_factory: SessionFactory, memory: MemoryStore, clock: FrozenClock
) -> None:
    items = [
        FactInput("client", "Acme Ltd", "budget", "KES 300,000 for a new website", source="t"),
        FactInput("preference", "owner", "coffee order", "flat white, no sugar", source="t"),
        FactInput("schedule", "owner", "gym", "Tuesdays and Thursdays at 6am", source="t"),
    ]
    async with transaction(session_factory) as session:
        for item in items:
            await memory.add(session, item, actor="t")
        await memory.add_episode(
            session, summary="Discussed the Acme website budget and timeline", conversation_id=None
        )
    async with session_factory() as session:
        hits = await search_facts(
            session, memory.embedder, "What is Acme's website budget?", now=clock.now()
        )
        episodes = await search_episodes(session, memory.embedder, "Acme budget")
    assert hits[0].fact.subject == "Acme Ltd"
    assert "KES 300,000" in render_facts(hits[:1])
    assert "inferred" in render_facts(hits[:1])
    assert episodes[0].episode.summary.startswith("Discussed the Acme")


async def test_unknown_category_is_refused(
    session_factory: SessionFactory, memory: MemoryStore
) -> None:
    async with transaction(session_factory) as session:
        with pytest.raises(MemoryStoreError, match="unknown memory category"):
            await memory.add(session, FactInput("gossip", "a", "b", "c", source="t"), actor="t")
