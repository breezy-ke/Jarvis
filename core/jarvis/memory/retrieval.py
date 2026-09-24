"""Hybrid memory search: vector similarity + full-text, fused by rank.

Reciprocal Rank Fusion (RRF) merges the two result lists without tuning
score scales. Small boosts favour confirmed facts and recent updates, so
what the owner told Jarvis outranks what Jarvis guessed.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import Episode, Fact
from jarvis.memory.embeddings import Embedder
from jarvis.memory.store import ACTIVE_STATUSES, FactStatus

_RRF_K = 60
_POOL = 40
SENSITIVE = "sensitive"  # Fact.sensitivity for health, money, family and credential details


@dataclass(frozen=True)
class ScoredFact:
    fact: Fact
    score: float


@dataclass(frozen=True)
class ScoredEpisode:
    episode: Episode
    score: float


def _recency_boost(updated: datetime, now: datetime) -> float:
    age_days = max((now - updated).total_seconds() / 86_400, 0.0)
    return 0.002 * math.exp(-age_days / 90)


async def search_facts(
    session: AsyncSession,
    embedder: Embedder,
    query: str,
    *,
    now: datetime,
    limit: int = 8,
    include_inferred: bool = True,
    categories: tuple[str, ...] | None = None,
    exclude_sensitive: bool = False,
) -> list[ScoredFact]:
    query = query.strip()
    if not query:
        return []
    statuses = (
        [s.value for s in ACTIVE_STATUSES] if include_inferred else [FactStatus.CONFIRMED.value]
    )
    [vector] = await embedder.embed([query])

    base = select(Fact).where(Fact.valid_to.is_(None)).where(Fact.status.in_(statuses))
    if categories:
        base = base.where(Fact.category.in_(categories))
    if exclude_sensitive:
        base = base.where(Fact.sensitivity != SENSITIVE)

    by_vector = list(
        await session.scalars(
            base.where(Fact.embedding.is_not(None))
            .order_by(Fact.embedding.cosine_distance(vector))
            .limit(_POOL)
        )
    )
    tsquery = func.websearch_to_tsquery("english", query)
    by_text = list(
        await session.scalars(
            base.where(Fact.search_tsv.op("@@")(tsquery))
            .order_by(func.ts_rank(Fact.search_tsv, tsquery).desc())
            .limit(_POOL)
        )
    )

    scores: dict[uuid.UUID, float] = {}
    facts: dict[uuid.UUID, Fact] = {}
    for ranked in (by_vector, by_text):
        for rank, fact in enumerate(ranked):
            facts[fact.id] = fact
            scores[fact.id] = scores.get(fact.id, 0.0) + 1.0 / (_RRF_K + rank + 1)
    for fact_id, fact in facts.items():
        if fact.status == FactStatus.CONFIRMED:
            scores[fact_id] += 0.003
        scores[fact_id] += _recency_boost(fact.updated_at, now)
    ordered = sorted(facts.values(), key=lambda f: scores[f.id], reverse=True)
    return [ScoredFact(f, scores[f.id]) for f in ordered[:limit]]


async def search_episodes(
    session: AsyncSession, embedder: Embedder, query: str, *, limit: int = 3
) -> list[ScoredEpisode]:
    query = query.strip()
    if not query:
        return []
    [vector] = await embedder.embed([query])
    distance = Episode.embedding.cosine_distance(vector)
    rows = (
        await session.execute(
            select(Episode, distance.label("distance"))
            .where(Episode.embedding.is_not(None))
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    return [ScoredEpisode(row[0], 1.0 - float(row[1])) for row in rows]


def render_facts(scored: list[ScoredFact]) -> str:
    if not scored:
        return "(nothing relevant in memory)"
    lines = []
    for item in scored:
        fact = item.fact
        tag = "" if fact.status == FactStatus.CONFIRMED else " (inferred, unconfirmed)"
        lines.append(f"- [{fact.category}] {fact.subject} — {fact.predicate}: {fact.value}{tag}")
    return "\n".join(lines)
