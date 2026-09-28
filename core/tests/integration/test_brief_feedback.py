"""Your 👍/👎 change tomorrow's brief, measurably, and "Why this?" says so."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from jarvis.brief.config import BriefConfig, parse_sources_config
from jarvis.brief.links import link_key
from jarvis.brief.rank import explain
from jarvis.brief.service import BriefService
from jarvis.clock import FrozenClock
from jarvis.db.models import Brief, BriefEntry, BriefItem
from jarvis.db.session import transaction
from jarvis.services import Services
from tests.fake_web import FakeWeb
from tests.integration.brief_helpers import MONDAY, at

pytestmark = pytest.mark.db

SOURCES = parse_sources_config(
    {
        "version": 1,
        "sources": [
            {"id": "verge", "name": "The Verge", "kind": "feed", "category": "news",
             "url": "https://www.theverge.com/rss/index.xml"},
            {"id": "techcrunch", "name": "TechCrunch", "kind": "feed", "category": "news",
             "url": "https://techcrunch.com/feed/"},
            {"id": "ars", "name": "Ars Technica", "kind": "feed", "category": "news",
             "url": "https://feeds.arstechnica.com/arstechnica/index"},
        ],
    }
)  # fmt: skip
NAMES = {s.id: s.name for s in SOURCES.sources}


async def store(services: Services, source: str, title: str, *, hours_ago: float) -> BriefItem:
    now = services.clock.now()
    url = f"https://{source}.test/{uuid.uuid4().hex[:8]}"
    [embedding] = await services.embedder.embed([f"{title}. "])
    item = BriefItem(
        id=uuid.uuid4(),
        key=link_key(url),
        url=url,
        source_id=source,
        kind="story",
        title=title,
        summary="",
        published_at=now - timedelta(hours=hours_ago),
        fetched_at=now,
        extra={},
        embedding=embedding,
    )
    async with transaction(services.session_factory) as session:
        session.add(item)
    return item


def topic(n: int) -> str:
    return " ".join(f"subject{n}part{k}" for k in range(5))  # nothing in common with others


async def ranking(service: BriefService) -> list[tuple[str, str, float, dict[str, Any]]]:
    """Tomorrow's ranking as it stands: (source, title, score, why), best first."""
    day = service.today()
    start, end = await service.builder.window(day)
    async with service._s.session_factory() as session:
        profile = (await service._s.profiles.current(session)).profile
    plan = await service.builder.plan(start, end, profile)
    return [(s.item.source_id, s.item.title, s.score, s.why) for s in [*plan.top, *plan.quick]]


async def test_your_votes_change_tomorrows_order(services: Services, clock: FrozenClock) -> None:
    service = BriefService(
        services, config=BriefConfig(), sources=SOURCES, fetcher=FakeWeb().fetcher()
    )
    # Monday: three stories from The Verge and three from TechCrunch.
    clock.set(at(MONDAY, 6, 40))
    liked_topic = topic(100)
    for n in range(3):
        await store(services, "verge", f"Verge {topic(n)}", hours_ago=2)
        await store(
            services,
            "techcrunch",
            f"TechCrunch {liked_topic if n == 0 else topic(10 + n)}",
            hours_ago=2,
        )
    monday = await service.builder.prepare(MONDAY)
    async with transaction(services.session_factory) as session:
        await session.execute(update(Brief).where(Brief.id == monday.id).values(status="delivered"))
        entries = list(
            await session.scalars(select(BriefEntry).where(BriefEntry.brief_id == monday.id))
        )
    assert len(entries) == 6

    # Tuesday's candidates: The Verge's are fresher, so without your votes they lead.
    clock.set(at(MONDAY + timedelta(days=1), 6, 40))
    for n in range(3):
        await store(services, "verge", f"Verge {topic(20 + n)}", hours_ago=1)
        await store(services, "techcrunch", f"TechCrunch {topic(30 + n)}", hours_ago=3)
    await store(services, "ars", f"Ars {liked_topic}", hours_ago=3)  # like one you'll 👍
    await store(services, "ars", f"Ars {topic(40)}", hours_ago=3)  # a neutral one
    before = await ranking(service)
    assert [source for source, *_ in before[:3]] == ["verge", "verge", "verge"]

    # Monday's brief: 👎 for The Verge, 👍 for TechCrunch.
    for entry in entries:
        await service.vote(entry.id, -1 if entry.source_id == "verge" else 1)

    after = await ranking(service)
    assert [source for source, *_ in after[:3]] == ["techcrunch"] * 3
    assert [source for source, *_ in after[-3:]] == ["verge"] * 3
    score_before = {title: score for _, title, score, _ in before}
    score_after = {title: score for _, title, score, _ in after}
    for source, title, _, _ in before:
        change = score_after[title] / score_before[title]
        if source == "techcrunch":
            assert change >= 2.0, (title, change)  # a source you like counts double
        if source == "verge":
            assert change <= 0.5, (title, change)  # one you don't, half

    # "Why this?" says what changed.
    why = {title: reasons for _, title, _, reasons in after}
    tc = next(title for source, title, *_ in after if source == "techcrunch")
    assert why[tc]["source_votes"] == 2.0
    assert "From a source you rate highly" in explain(why[tc], NAMES)
    similar, neutral = f"Ars {liked_topic}", f"Ars {topic(40)}"
    assert score_before[similar] == pytest.approx(score_before[neutral])  # a tie without votes
    assert score_after[similar] > score_after[neutral] * 1.4  # like a story you gave a 👍
    assert why[similar].get("liked_similar") is True
    assert "Like stories you gave a 👍" in explain(why[similar], NAMES)
    assert score_after[neutral] == pytest.approx(score_before[neutral])  # nothing to do with it

    # Take the votes back and it's as it was.
    for entry in entries:
        await service.vote(entry.id, None)
    restored = await ranking(service)
    assert {t: sc for _, t, sc, _ in restored} == pytest.approx(score_before)
