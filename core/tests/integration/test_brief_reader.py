"""Reading the brief's sources every hour: stories stored once, failures recorded, politeness."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from jarvis.brief.config import BriefConfig, parse_sources_config
from jarvis.brief.reader import BriefReader
from jarvis.clock import FrozenClock
from jarvis.db.models import BriefItem, BriefSource
from jarvis.db.session import transaction
from jarvis.services import Services
from tests.fake_web import FakeWeb

pytestmark = pytest.mark.db

SOURCES = parse_sources_config(
    {
        "version": 1,
        "sources": [
            {"id": "nextjs", "name": "Next.js", "kind": "feed", "category": "web",
             "url": "https://nextjs.org/feed.xml"},
            {"id": "techcabal", "name": "TechCabal", "kind": "feed", "category": "africa",
             "url": "https://techcabal.com/feed/"},
            {"id": "hn", "name": "Hacker News", "kind": "hn", "category": "news", "min_points": 100},
            {"id": "rising", "name": "Rising on GitHub", "kind": "github_rising",
             "category": "tools"},
            {"id": "advisories", "name": "Advisories", "kind": "github_advisories",
             "category": "security"},
            {"id": "kev", "name": "CISA KEV", "kind": "cisa_kev", "category": "security",
             "match": ["wordpress", "php"]},
        ],
    }
)  # fmt: skip


def advisory(package: str, ecosystem: str, severity: str, published: datetime) -> dict[str, Any]:
    return {
        "ghsa_id": f"GHSA-{package[:4]}-{severity[:4]}-0001",
        "cve_id": "CVE-2026-12345",
        "html_url": f"https://github.com/advisories/GHSA-{package[:4]}-{severity[:4]}-0001",
        "summary": f"{package} lets an attacker read files",
        "description": "Upgrade now.",
        "severity": severity,
        "published_at": published.isoformat(),
        "vulnerabilities": [
            {
                "package": {"ecosystem": ecosystem, "name": package},
                "vulnerable_version_range": "< 16.0.4",
                "first_patched_version": "16.0.4",
            }
        ],
    }


def the_web(now: datetime) -> FakeWeb:
    web = FakeWeb()
    web.feed(
        "https://nextjs.org/feed.xml",
        [
            {"title": "Next.js 16.1", "link": "https://nextjs.org/blog/next-16-1?utm_source=rss",
             "summary": "<p>Faster builds.</p>", "published": now - timedelta(hours=5)},
            {"title": "Next.js 12", "link": "https://nextjs.org/blog/next-12",
             "summary": "Old news.", "published": now - timedelta(days=30)},
        ],
        etag='"v1"',
    )  # fmt: skip
    web.feed(
        "https://techcabal.com/feed/",
        [{"title": "M-Pesa opens its API to startups", "link": "https://techcabal.com/mpesa",
          "summary": "Safaricom's new developer portal.", "published": now - timedelta(hours=2)}],
    )  # fmt: skip
    web.hn(
        [
            {"id": 1, "title": "Next.js 16.1 is out", "url": "https://nextjs.org/blog/next-16-1",
             "score": 420, "time": now - timedelta(hours=4)},
            {"id": 2, "title": "Show HN: A tiny database", "url": "https://tiny.test/db",
             "score": 150, "time": now - timedelta(hours=3)},
            {"id": 3, "title": "Not popular", "url": "https://quiet.test/", "score": 20,
             "time": now - timedelta(hours=1)},
        ]
    )  # fmt: skip
    web.github_rising(
        [{"full_name": "acme/rocket", "html_url": "https://github.com/acme/rocket",
          "description": "Deploy anything", "stargazers_count": 900, "language": "Go",
          "created_at": (now - timedelta(days=2)).isoformat()}]
    )  # fmt: skip
    web.advisories(
        [
            advisory("next", "npm", "high", now - timedelta(hours=10)),
            advisory("next", "npm", "low", now - timedelta(hours=10)),  # below what you asked for
            advisory("lodash", "npm", "critical", now - timedelta(hours=10)),  # not your stack
            advisory("laravel/framework", "composer", "critical", now - timedelta(hours=8)),
        ]
    )
    web.kev(
        [
            {"cveID": "CVE-2026-1111", "vendorProject": "WordPress", "product": "WordPress",
             "vulnerabilityName": "WordPress Core SQL Injection", "dateAdded": now.date().isoformat(),
             "shortDescription": "SQL injection.", "requiredAction": "Update to 6.8.3.",
             "dueDate": "2026-10-19", "knownRansomwareCampaignUse": "Unknown"},
            {"cveID": "CVE-2026-2222", "vendorProject": "Microsoft", "product": "Exchange",
             "vulnerabilityName": "Exchange RCE", "dateAdded": now.date().isoformat(),
             "shortDescription": "RCE.", "requiredAction": "Patch.", "dueDate": "2026-10-19",
             "knownRansomwareCampaignUse": "Known"},
            {"cveID": "CVE-2025-3333", "vendorProject": "PHP", "product": "PHP",
             "vulnerabilityName": "Old PHP bug", "dateAdded": "2025-01-01",
             "shortDescription": "Old.", "requiredAction": "Update.", "dueDate": "2025-02-01",
             "knownRansomwareCampaignUse": "Unknown"},
        ]
    )  # fmt: skip
    return web


async def your_stacks(services: Services, *stacks: str) -> None:
    async with transaction(services.session_factory) as session:
        await services.profiles.update(
            session, {"engineering.primary_stacks": list(stacks)}, created_by="owner"
        )


async def stored(services: Services) -> dict[str, BriefItem]:
    async with services.session_factory() as session:
        return {item.url: item for item in await session.scalars(select(BriefItem))}


async def health(services: Services, source_id: str) -> BriefSource:
    async with services.session_factory() as session:
        state = await session.get(BriefSource, source_id)
    assert state is not None
    return state


async def test_every_source_is_read_and_each_story_stored_once(
    services: Services, clock: FrozenClock
) -> None:
    await your_stacks(services, "Next.js", "Laravel")
    web = the_web(clock.now())
    reader = BriefReader(services, config=BriefConfig(), sources=SOURCES, fetcher=web.fetcher())

    report = await reader.read_round()
    assert sorted(report.read) == sorted(s.id for s in SOURCES.sources)
    assert report.failed == {}

    items = await stored(services)
    article = items["https://nextjs.org/blog/next-16-1"]
    assert article.source_id in ("nextjs", "hn")  # whichever came first
    assert article.extra["hn"]["points"] == 420  # Hacker News's copy joined the article
    assert article.summary == "Faster builds."
    assert article.embedding is not None
    assert "https://nextjs.org/blog/next-12" not in items  # older than the window
    assert "https://quiet.test/" not in items  # too few points
    assert "https://tiny.test/db" in items
    assert items["https://github.com/acme/rocket"].extra["repo"]["stars"] == 900

    advisories = {i.title: i for i in items.values() if i.kind == "advisory"}
    assert sorted(a.extra["advisory"]["severity"] for a in advisories.values()) == [
        "critical",
        "high",
    ]  # next (high) and laravel/framework (critical): your stacks, your severities
    [next_js] = [a for a in advisories.values() if a.title.startswith("next:")]
    assert next_js.extra["advisory"]["packages"] == [
        {"ecosystem": "npm", "name": "next", "vulnerable": "< 16.0.4", "patched": "16.0.4"}
    ]
    exploited = [i for i in items.values() if i.kind == "exploited"]
    assert [e.extra["exploited"]["cve"] for e in exploited] == ["CVE-2026-1111"]
    assert exploited[0].url == "https://nvd.nist.gov/vuln/detail/CVE-2026-1111"

    # Nothing new next time: every story is already stored once.
    clock.advance(hours=1)
    assert (await reader.read_round()).new_items == 0
    assert len(await stored(services)) == len(items)


async def test_a_failing_source_is_recorded_and_tried_again(
    services: Services, clock: FrozenClock
) -> None:
    web = the_web(clock.now())
    web.page("https://techcabal.com/feed/", "Down for maintenance", status=503)
    reader = BriefReader(services, config=BriefConfig(), sources=SOURCES, fetcher=web.fetcher())

    report = await reader.read_round()
    assert set(report.failed) == {"techcabal"}
    state = await health(services, "techcabal")
    assert (state.failures, state.last_ok_at) == (1, None)
    assert "503" in (state.last_error or "")

    web.feed("https://techcabal.com/feed/", [])  # back up
    clock.advance(hours=1)
    await reader.read_round()
    state = await health(services, "techcabal")
    assert (state.failures, state.last_error) == (0, None)
    assert state.last_ok_at == clock.now()


async def test_sources_are_read_when_due_and_asked_politely(
    services: Services, clock: FrozenClock
) -> None:
    web = the_web(clock.now())
    reader = BriefReader(services, config=BriefConfig(), sources=SOURCES, fetcher=web.fetcher())
    await reader.read_round()
    assert web.hits("nextjs.org", "/feed.xml") == 1

    clock.advance(minutes=20)
    report = await reader.read_round()
    assert "nextjs" in report.not_due  # read every hour, not every round
    assert web.hits("nextjs.org", "/feed.xml") == 1

    clock.advance(hours=1)
    await reader.read_round()
    assert web.hits("nextjs.org", "/feed.xml") == 2
    state = await health(services, "nextjs")
    assert state.etag == '"v1"'  # the feed said it hadn't changed (304), and that's fine
    assert state.last_ok_at == clock.now()
    assert "rising" in (await reader.read_round(force=True)).read  # "read everything now"


async def test_old_stories_and_article_text_are_forgotten(
    services: Services, clock: FrozenClock
) -> None:
    web = the_web(clock.now())
    reader = BriefReader(services, config=BriefConfig(), sources=SOURCES, fetcher=web.fetcher())
    await reader.read_round()
    async with transaction(services.session_factory) as session:
        for item in await session.scalars(select(BriefItem)):
            item.text = "The whole article."

    clock.advance(days=8)
    assert await reader.purge() == 0  # stories stay a month
    assert all(item.text is None for item in (await stored(services)).values())
    clock.advance(days=30)
    everything = len(await stored(services))
    assert everything > 0
    assert await reader.purge() == everything
    assert await stored(services) == {}
