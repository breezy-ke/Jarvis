"""The fact store: what Jarvis remembers, with provenance and history.

Rules:
  * One *current* fact per (subject, predicate). New information supersedes
    the old fact (sets its `valid_to`) instead of overwriting it.
  * `inferred` facts (extracted by a model, or from ingestion) wait for the
    owner to confirm or reject them. `confirmed` facts came from the owner.
  * "Forget" works in two ways. Chat does a soft forget: the fact is hidden
    at once and purged after 7 days. The memory page does a hard delete:
    immediate and permanent.
  * The audit log records fact IDs and categories, never fact content, so
    forgetting something never conflicts with the tamper-evident log.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.log import AuditLog
from jarvis.clock import Clock
from jarvis.db.models import Episode, Fact
from jarvis.memory.embeddings import Embedder


class FactStatus(StrEnum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    REJECTED = "rejected"
    FORGOTTEN = "forgotten"
    SUPERSEDED = "superseded"


ACTIVE_STATUSES = (FactStatus.CONFIRMED, FactStatus.INFERRED)
CATEGORIES = (
    "identity",
    "business",
    "client",
    "project",
    "preference",
    "contact",
    "schedule",
    "goal",
    "skill",
    "note",
    "profile_suggestion",
)


@dataclass(frozen=True)
class FactInput:
    category: str
    subject: str
    predicate: str
    value: str
    source: str
    status: FactStatus = FactStatus.INFERRED
    confidence: float = 0.7
    sensitivity: str = "normal"
    source_ref: str | None = None


class MemoryStoreError(ValueError):
    pass


def _norm(text: str) -> str:
    return " ".join(text.strip().split())


def fact_text(fact: Fact | FactInput) -> str:
    return f"{fact.subject} {fact.predicate} {fact.value}"


class MemoryStore:
    def __init__(self, *, embedder: Embedder, audit: AuditLog, clock: Clock) -> None:
        self.embedder = embedder
        self._audit = audit
        self._clock = clock

    async def add(self, session: AsyncSession, item: FactInput, *, actor: str) -> Fact:
        if item.category not in CATEGORIES:
            raise MemoryStoreError(f"unknown memory category {item.category!r}")
        subject, predicate, value = _norm(item.subject), _norm(item.predicate), _norm(item.value)
        if not (subject and predicate and value):
            raise MemoryStoreError("a fact needs a subject, a predicate and a value")
        now = self._clock.now()
        current = await session.scalar(
            select(Fact)
            .where(func.lower(Fact.subject) == subject.lower())
            .where(func.lower(Fact.predicate) == predicate.lower())
            .where(Fact.valid_to.is_(None))
            .where(Fact.status.in_([s.value for s in ACTIVE_STATUSES]))
            .with_for_update()
        )
        if current is not None and current.value.lower() == value.lower():
            # Same fact again: reinforce it (and upgrade to confirmed if the owner said it).
            current.confidence = max(current.confidence, item.confidence)
            if item.status == FactStatus.CONFIRMED:
                current.status = FactStatus.CONFIRMED
            current.updated_at = now
            return current
        if current is not None:
            if current.status == FactStatus.CONFIRMED and item.status == FactStatus.INFERRED:
                # Never let a guess silently override what the owner confirmed:
                # store it alongside as a suggestion instead.
                predicate = f"{predicate} (suggested update)"
            else:
                current.valid_to = now
                current.status = FactStatus.SUPERSEDED
                current.updated_at = now
        [embedding] = await self.embedder.embed([f"{subject} {predicate} {value}"])
        fact = Fact(
            id=uuid.uuid4(),
            category=item.category,
            subject=subject,
            predicate=predicate,
            value=value,
            sensitivity=item.sensitivity,
            status=item.status.value,
            source=item.source,
            source_ref=item.source_ref,
            confidence=item.confidence,
            valid_from=now,
            created_at=now,
            updated_at=now,
            embedding=embedding,
        )
        session.add(fact)
        await session.flush()
        await self._audit.append(
            session,
            actor=actor,
            event_type="memory.added",
            subject_type="fact",
            subject_id=str(fact.id),
            summary=f"Remembered a {item.category} fact ({item.status.value})",
            data={
                "category": item.category,
                "status": item.status.value,
                "source": item.source,
                "superseded": str(current.id) if current is not None else None,
            },
        )
        return fact

    async def _get_active(self, session: AsyncSession, fact_id: uuid.UUID) -> Fact:
        fact = await session.get(Fact, fact_id, with_for_update=True)
        if fact is None or fact.valid_to is not None:
            raise MemoryStoreError("That memory no longer exists.")
        return fact

    async def confirm(
        self, session: AsyncSession, fact_id: uuid.UUID, *, actor: str = "owner"
    ) -> Fact:
        fact = await self._get_active(session, fact_id)
        fact.status = FactStatus.CONFIRMED
        fact.confidence = 1.0
        fact.updated_at = self._clock.now()
        await self._audit.append(
            session,
            actor=actor,
            event_type="memory.confirmed",
            subject_type="fact",
            subject_id=str(fact.id),
            summary=f"Confirmed a {fact.category} fact",
            data={"category": fact.category},
        )
        return fact

    async def reject(
        self, session: AsyncSession, fact_id: uuid.UUID, *, actor: str = "owner"
    ) -> Fact:
        fact = await self._get_active(session, fact_id)
        now = self._clock.now()
        fact.status = FactStatus.REJECTED
        fact.valid_to = now
        fact.updated_at = now
        await self._audit.append(
            session,
            actor=actor,
            event_type="memory.rejected",
            subject_type="fact",
            subject_id=str(fact.id),
            summary=f"Rejected a {fact.category} fact",
            data={"category": fact.category},
        )
        return fact

    async def edit(
        self, session: AsyncSession, fact_id: uuid.UUID, value: str, *, actor: str = "owner"
    ) -> Fact:
        old = await self._get_active(session, fact_id)
        return await self.add(
            session,
            FactInput(
                category=old.category,
                subject=old.subject,
                predicate=old.predicate.replace(" (suggested update)", ""),
                value=value,
                source="owner:edit",
                status=FactStatus.CONFIRMED,
                confidence=1.0,
                sensitivity=old.sensitivity,
            ),
            actor=actor,
        )

    async def forget(
        self, session: AsyncSession, fact_id: uuid.UUID, *, hard: bool, actor: str = "owner"
    ) -> bool:
        """Forget one fact. Returns False if it didn't exist."""
        fact = await session.get(Fact, fact_id, with_for_update=True)
        if fact is None:
            return False
        category = fact.category
        if hard:
            await session.delete(fact)
        else:
            now = self._clock.now()
            fact.status = FactStatus.FORGOTTEN
            fact.valid_to = now
            fact.updated_at = now
        await self._audit.append(
            session,
            actor=actor,
            event_type="memory.deleted" if hard else "memory.forgotten",
            subject_type="fact",
            subject_id=str(fact_id),
            summary=f"{'Deleted' if hard else 'Forgot'} a {category} fact",
            data={"category": category, "hard": hard},
        )
        return True

    async def retire(
        self, session: AsyncSession, *, subject: str, predicate: str, actor: str
    ) -> int:
        """Mark the current fact for (subject, predicate) as no longer true."""
        now = self._clock.now()
        rows = list(
            await session.scalars(
                select(Fact)
                .where(func.lower(Fact.subject) == _norm(subject).lower())
                .where(func.lower(Fact.predicate) == _norm(predicate).lower())
                .where(Fact.valid_to.is_(None))
                .with_for_update()
            )
        )
        for fact in rows:
            fact.valid_to = now
            fact.status = FactStatus.SUPERSEDED
            fact.updated_at = now
            await self._audit.append(
                session,
                actor=actor,
                event_type="memory.retired",
                subject_type="fact",
                subject_id=str(fact.id),
                summary=f"A {fact.category} fact is no longer true",
                data={"category": fact.category},
            )
        return len(rows)

    async def purge_forgotten(self, session: AsyncSession, *, older_than: timedelta) -> int:
        cutoff = self._clock.now() - older_than
        result = await session.execute(
            delete(Fact)
            .where(Fact.status == FactStatus.FORGOTTEN)
            .where(Fact.updated_at < cutoff)
            .returning(Fact.id)
        )
        purged = len(result.all())
        if purged:
            await self._audit.append(
                session,
                actor="system",
                event_type="memory.purged",
                summary=f"Permanently removed {purged} forgotten fact(s)",
                data={"count": purged},
            )
        return purged

    async def list(
        self,
        session: AsyncSession,
        *,
        statuses: tuple[FactStatus, ...] = ACTIVE_STATUSES,
        category: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Fact]:
        stmt = select(Fact).where(Fact.status.in_([s.value for s in statuses]))
        if category:
            stmt = stmt.where(Fact.category == category)
        if query:
            like = f"%{query.strip()}%"
            stmt = stmt.where(
                or_(Fact.subject.ilike(like), Fact.predicate.ilike(like), Fact.value.ilike(like))
            )
        stmt = stmt.order_by(Fact.updated_at.desc()).limit(limit).offset(offset)
        return list(await session.scalars(stmt))

    async def add_episode(
        self, session: AsyncSession, *, summary: str, conversation_id: uuid.UUID | None
    ) -> Episode:
        [embedding] = await self.embedder.embed([summary])
        episode = Episode(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            summary=_norm(summary),
            created_at=self._clock.now(),
            embedding=embedding,
        )
        session.add(episode)
        await session.flush()
        return episode
