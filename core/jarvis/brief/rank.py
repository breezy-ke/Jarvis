"""Ranking the brief's stories: by code, locally, and able to say why.

score = source weight × freshness × relevance to you × your feedback × boosts

* **Freshness** halves every 18 hours.
* **Relevance** compares a story's embedding with an interest profile built
  from your stacks, services, clients and goals. Both are computed on this
  PC: your profile isn't sent anywhere for this.
* **Your feedback** (👍/👎): each source's weight moves between ×0.5 and ×2,
  and stories like ones you liked (or disliked) lately rise (or fall).
* **Boosts:** Hacker News points, and naming one of your stacks.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jarvis.profile.schema import Profile

HALF_LIFE_HOURS = 18.0
SAME_STORY = 0.92  # embeddings this close are the same story from two places
SOURCE_RANGE = (0.5, 2.0)
TASTE_WEIGHT = 0.6


def interest_text(profile: Profile) -> str:
    """What you work on and care about, from your profile (used only on this PC)."""
    e, b, c, g = profile.engineering, profile.business, profile.clients, profile.goals
    parts: list[str] = [
        *e.primary_stacks,
        *e.also_uses,
        *e.hosting,
        *b.services,
        *(client.description or "" for client in c.ideal_clients),
        *g.business_goals,
        *g.learning_goals,
    ]
    return ". ".join(part.strip() for part in parts if part.strip())


def stack_terms(profile: Profile) -> list[str]:
    """Your stacks as words to spot in a headline: "Next.js", "Laravel"…"""
    terms = [*profile.engineering.primary_stacks, *profile.engineering.also_uses]
    return sorted({t.strip() for t in terms if 2 <= len(t.strip()) <= 30}, key=str.lower)


def cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    if a is None or b is None or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _mentions(text: str, term: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(term.lower())}(?![\w])", text.lower()) is not None


@dataclass
class Taste:
    """What your 👍/👎 taught Jarvis: per-source multipliers and recent examples."""

    sources: dict[str, float] = field(default_factory=dict)
    liked: list[Sequence[float]] = field(default_factory=list)
    disliked: list[Sequence[float]] = field(default_factory=list)

    @classmethod
    def from_votes(cls, votes: Iterable[tuple[str, int, Sequence[float] | None]]) -> Taste:
        """`votes`: (source id, +1 or -1, the story's embedding), most recent first."""
        ups: dict[str, int] = {}
        downs: dict[str, int] = {}
        taste = cls()
        for source_id, vote, embedding in votes:
            (ups if vote > 0 else downs)[source_id] = (ups if vote > 0 else downs).get(
                source_id, 0
            ) + 1
            if embedding is not None:
                (taste.liked if vote > 0 else taste.disliked).append(embedding)
        low, high = SOURCE_RANGE
        for source_id in {*ups, *downs}:
            ratio = (1 + ups.get(source_id, 0)) / (1 + downs.get(source_id, 0))
            taste.sources[source_id] = min(high, max(low, ratio))
        return taste

    def similarity(self, embedding: Sequence[float] | None) -> tuple[float, float]:
        liked = max((cosine(embedding, v) for v in self.liked), default=0.0)
        disliked = max((cosine(embedding, v) for v in self.disliked), default=0.0)
        return max(0.0, liked), max(0.0, disliked)


@dataclass
class Scored:
    item: Any  # a BriefItem
    score: float
    why: dict[str, Any]
    also: list[str] = field(default_factory=list)  # other sources with the same story


def score(
    item: Any,
    *,
    now: datetime,
    source_weight: float,
    interest: Sequence[float] | None,
    stacks: Sequence[str],
    taste: Taste,
) -> Scored:
    age_hours = max(0.0, (now - item.published_at).total_seconds() / 3600)
    fresh = 0.5 ** (age_hours / HALF_LIFE_HOURS)
    relevance = max(0.0, cosine(item.embedding, interest))
    votes = taste.sources.get(item.source_id, 1.0)
    liked, disliked = taste.similarity(item.embedding)
    taste_factor = max(0.25, 1.0 + TASTE_WEIGHT * (liked - disliked))
    text = f"{item.title} {item.summary}"
    named = [term for term in stacks if _mentions(text, term)]
    points = int((item.extra or {}).get("hn", {}).get("points") or 0)
    boost = (1.3 if named else 1.0) * (1.0 + 0.08 * math.log10(1 + points))
    total = source_weight * votes * fresh * (0.5 + relevance) * taste_factor * boost
    why: dict[str, Any] = {
        "source": round(source_weight * votes, 3),
        "fresh": round(fresh, 3),
        "relevance": round(relevance, 3),
        "taste": round(taste_factor, 3),
    }
    if named:
        why["stack"] = named
    if points:
        why["points"] = points
    if votes != 1.0:
        why["source_votes"] = round(votes, 3)
    if liked >= 0.5 and liked > disliked:
        why["liked_similar"] = True
    if disliked >= 0.5 and disliked > liked:
        why["disliked_similar"] = True
    return Scored(item=item, score=total, why=why)


def merge_same_stories(ranked: list[Scored]) -> list[Scored]:
    """Keep the best-scored copy of each story; note where else it appeared."""
    kept: list[Scored] = []
    for candidate in sorted(ranked, key=lambda s: s.score, reverse=True):
        twin = next(
            (k for k in kept if cosine(k.item.embedding, candidate.item.embedding) >= SAME_STORY),
            None,
        )
        if twin is None:
            kept.append(candidate)
        elif candidate.item.source_id != twin.item.source_id:
            twin.also.append(candidate.item.source_id)
    return kept


def explain(why: dict[str, Any], source_names: dict[str, str]) -> list[str]:
    """ "Why this?" in plain words, strongest reasons first."""
    reasons: list[str] = []
    if why.get("stack"):
        reasons.append("Mentions " + ", ".join(why["stack"][:3]))
    if why.get("liked_similar"):
        reasons.append("Like stories you gave a 👍")
    if why.get("relevance", 0) >= 0.35:
        reasons.append("Close to your work")
    if why.get("points"):
        reasons.append(f"{why['points']} points on Hacker News")
    if why.get("source_votes", 1.0) > 1.0:
        reasons.append("From a source you rate highly")
    also = [str(s) for s in why.get("also", [])[:2]]
    if also:
        reasons.append("Also in " + ", ".join(source_names.get(s, s) for s in also))
    if why.get("disliked_similar"):
        reasons.append("Though similar to stories you gave a 👎")
    return reasons or ["Fresh from a source you follow"]
