"""Building the morning brief: ranked for you, linked to its sources, private where it matters."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from jarvis.brief.build import BriefBuilder
from jarvis.brief.config import BriefConfig, parse_sources_config
from jarvis.brief.links import link_key
from jarvis.brief.render import build_view
from jarvis.clock import FrozenClock
from jarvis.db.models import Brief, BriefEntry, BriefItem
from jarvis.db.session import SessionFactory, transaction
from jarvis.llm.config import parse_models_config
from jarvis.memory.embeddings import HashEmbedder
from jarvis.services import Services, build_services
from tests.conftest import FAKE_TASKS, make_settings
from tests.fake_web import FakeWeb
from tests.profile_helpers import FULL_PROFILE

pytestmark = pytest.mark.db

SOURCES = parse_sources_config(
    {
        "version": 1,
        "sources": [
            {"id": "nextjs", "name": "Next.js", "kind": "feed", "category": "web",
             "url": "https://nextjs.org/feed.xml"},
            {"id": "css", "name": "CSS Weekly", "kind": "feed", "category": "web",
             "url": "https://css.test/feed", "weight": 0.8},
            {"id": "verge", "name": "The Verge", "kind": "feed", "category": "news",
             "url": "https://www.theverge.com/rss/index.xml"},
            {"id": "techcrunch", "name": "TechCrunch", "kind": "feed", "category": "news",
             "url": "https://techcrunch.com/feed/"},
            {"id": "techcabal", "name": "TechCabal", "kind": "feed", "category": "africa",
             "url": "https://techcabal.com/feed/"},
            {"id": "hn", "name": "Hacker News", "kind": "hn", "category": "news"},
            {"id": "advisories", "name": "GitHub advisories", "kind": "github_advisories",
             "category": "security"},
            {"id": "kev", "name": "CISA KEV", "kind": "cisa_kev", "category": "security"},
        ],
    }
)  # fmt: skip

# Your profile, in words that never appear in the news below: if any of them reaches the
# public model, it came from your profile.
PRIVATE_WORDS = (
    "Breezy Digital",
    "Nairobi SMEs with outdated sites",
    "3 new retainers this quarter",
    "Clinic site",
    "warm and concise",
    "dry British wit",
    "Achieng",
    "Brian",
)


def asked(messages: list[ModelMessage]) -> str:
    return "\n".join(
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


@dataclass
class Writers:
    """Stands in for both of the brief's writers, and keeps everything each was sent."""

    summaries: list[str] = field(default_factory=list)  # everything sent, as the model saw it
    notes: list[str] = field(default_factory=list)
    sent: dict[str, list[str]] = field(default_factory=dict)  # by model: all it was sent
    down: bool = False

    def install(self, services: Services) -> Writers:
        for ref in ("fake-local", "fake-public"):
            services.router._models[ref] = FunctionModel(self._as(ref))
        return self

    def _as(self, ref: str) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
        def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.sent.setdefault(ref, []).append(repr(messages))
            return self(messages, info)

        return answer

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if self.down:
            raise ConnectionError("no model is reachable")
        output = info.output_tools[0]
        prompt = asked(messages)
        count = len(re.findall(r"^\d+\.$", prompt, re.MULTILINE))
        answer: dict[str, Any]
        if "do_today" in output.parameters_json_schema.get("properties", {}):
            self.notes.append(repr(messages))
            answer = {
                "items": [
                    {
                        "index": n,
                        "why": f"Why story {n} matters to you, see https://evil.test/{n}",
                        "client": "Pitch it at www.evil.test" if n == 1 else "",
                    }
                    for n in range(1, count + 1)
                ],
                "do_today": "Upgrade next in your client projects: https://evil.test/do",
                "script": "Good morning. Details at https://evil.test/s. That's your brief.",
            }
        else:
            self.summaries.append(repr(messages))
            answer = {
                "items": [
                    {"index": n, "summary": f"Model summary {n}. More at https://evil.test/{n}"}
                    for n in range(1, count + 1)
                ]
                + [{"index": 99, "summary": "A story that isn't there."}]
            }
        return ModelResponse(parts=[ToolCallPart(output.name, answer)])


async def with_profile(services: Services) -> None:
    async with transaction(services.session_factory) as session:
        await services.profiles.update(session, FULL_PROFILE, created_by="owner")


async def store(services: Services, *stories: dict[str, Any]) -> dict[str, BriefItem]:
    """Stories as the hourly reader would have stored them."""
    now = services.clock.now()
    rows: dict[str, BriefItem] = {}
    async with transaction(services.session_factory) as session:
        for story in stories:
            summary = story.get("summary", "")
            [embedding] = await services.embedder.embed([f"{story['title']}. {summary[:500]}"])
            row = BriefItem(
                id=uuid.uuid4(),
                key=link_key(story["url"]),
                url=story["url"],
                source_id=story["source"],
                kind=story.get("kind", "story"),
                title=story["title"],
                summary=summary,
                published_at=now - timedelta(hours=story.get("hours_ago", 2)),
                fetched_at=story.get("fetched_at", now),
                extra=story.get("extra", {}),
                embedding=embedding,
            )
            session.add(row)
            rows[row.url] = row
    return rows


def the_news() -> list[dict[str, Any]]:
    return [
        {"source": "nextjs", "url": "https://nextjs.org/blog/next-16-1",
         "title": "Next.js 16.1", "summary": "Faster builds with Turbopack."},
        {"source": "css", "url": "https://css.test/anchor",
         "title": "Anchor positioning lands everywhere", "summary": "A CSS feature."},
        {"source": "verge", "url": "https://www.theverge.com/phones/1",
         "title": "A new phone has a bigger camera bump",
         "summary": "The phone ships in October with a bigger camera bump."},
        {"source": "techcrunch", "url": "https://techcrunch.com/phones/1",
         "title": "A new phone has a bigger camera bump",
         "summary": "The phone ships in October with a bigger camera bump."},
        {"source": "hn", "url": "https://tiny.test/db", "title": "Show HN: A tiny database",
         "extra": {"hn": {"points": 480, "comments": 120,
                          "discussion": "https://news.ycombinator.com/item?id=2"}}},
        {"source": "techcabal", "url": "https://techcabal.com/mpesa",
         "title": "M-Pesa opens its API to startups", "summary": "Safaricom's developer portal."},
        {"source": "techcabal", "url": "https://techcabal.com/funding",
         "title": "A Lagos fintech raises $20m", "summary": "Series A.", "hours_ago": 20},
        {"source": "verge", "url": "https://www.theverge.com/games/2",
         "title": "A game studio closes", "summary": "Layoffs.", "hours_ago": 30},
        {"source": "verge", "url": "https://www.theverge.com/cars/3",
         "title": "An electric car recall", "summary": "Software fix.", "hours_ago": 40},
        {"source": "css", "url": "https://css.test/old", "title": "Too old to matter",
         "hours_ago": 80},
        {"source": "advisories", "kind": "advisory",
         "url": "https://github.com/advisories/GHSA-next-high-0001",
         "title": "next: Next.js lets an attacker read files", "summary": "Upgrade now. Really.",
         "extra": {"advisory": {"ghsa": "GHSA-next-high-0001", "cve": "CVE-2026-12345",
                                "severity": "high", "packages": [
                                    {"ecosystem": "npm", "name": "next",
                                     "vulnerable": "< 16.0.4", "patched": "16.0.4"}]}}},
        {"source": "advisories", "kind": "advisory",
         "url": "https://github.com/advisories/GHSA-lara-crit-0001",
         "title": "laravel/framework: SQL injection", "summary": "Patch.", "hours_ago": 6,
         "extra": {"advisory": {"ghsa": "GHSA-lara-crit-0001", "cve": None,
                                "severity": "critical", "packages": [
                                    {"ecosystem": "composer", "name": "laravel/framework",
                                     "vulnerable": ">= 11.0, < 11.9.2", "patched": "11.9.2"}]}}},
        {"source": "kev", "kind": "exploited",
         "url": "https://nvd.nist.gov/vuln/detail/CVE-2026-1111",
         "title": "WordPress WordPress: WordPress Core SQL Injection", "summary": "SQL injection.",
         "hours_ago": 9,
         "extra": {"exploited": {"cve": "CVE-2026-1111", "product": "WordPress WordPress",
                                 "action": "Update to 6.8.3.", "due": "2026-10-19",
                                 "ransomware": False}}},
    ]  # fmt: skip


def articles() -> FakeWeb:
    web = FakeWeb()
    web.page(
        "https://nextjs.org/blog/next-16-1",
        "<html><body><article><h1>Next.js 16.1</h1><p>"
        + "Turbopack builds are now twice as fast for large apps. " * 8
        + "</p></article></body></html>",
    )
    web.page("https://css.test/robots.txt", "User-agent: *\nDisallow: /", content_type="text/plain")
    return web


def builder(services: Services, web: FakeWeb) -> BriefBuilder:
    return BriefBuilder(services, config=BriefConfig(), sources=SOURCES, fetcher=web.fetcher())


async def entries_of(services: Services, brief: Brief) -> list[BriefEntry]:
    async with services.session_factory() as session:
        return list(
            await session.scalars(
                select(BriefEntry)
                .where(BriefEntry.brief_id == brief.id)
                .order_by(BriefEntry.section, BriefEntry.rank)
            )
        )


def today(services: Services) -> date:
    return services.clock.now().astimezone(services.policies_config.tz).date()


async def test_the_brief_is_ranked_for_you_and_every_item_links_to_its_source(
    services: Services,
) -> None:
    await with_profile(services)
    stored = await store(services, *the_news())
    writers = Writers().install(services)
    web = articles()
    build = builder(services, web)

    brief = await build.prepare(today(services))
    assert brief.status == "ready"
    assert brief.models == {"summaries": "fake-local", "notes": "fake-local"}
    local = brief.scheduled_for.astimezone(services.policies_config.tz)
    assert (local.date(), local.hour, local.minute) == (today(services), 7, 0)

    view = build_view(
        brief,
        await entries_of(services, brief),
        decrypt=build.dec,
        source_names={s.id: s.name for s in SOURCES.sources},
    )
    top = view.section("top")
    assert top[0].title == "Next.js 16.1"  # your stack, and close to your work
    assert top[0].reasons[0] == "Mentions Next.js"
    assert len(top) == 5
    # The phone story was in two places: it's shown once, and says where else.
    phones = [e for e in view.entries if "camera bump" in e.title]
    assert len(phones) == 1
    assert any(reason.startswith("Also in ") for reason in phones[0].reasons)
    africa = view.section("africa")
    assert africa
    assert all(e.source == "TechCabal" for e in africa)
    assert len({e.url for e in view.entries}) == len(view.entries)  # nothing twice
    assert "Too old to matter" not in [e.title for e in view.entries]

    # Every item links to its source's own address, whatever the models said.
    for shown in view.entries:
        item = stored[shown.url]
        assert shown.url == item.url
        assert shown.source == SOURCES.get(item.source_id).name  # type: ignore[union-attr]
    everything = " ".join(
        [view.do_today, view.script] + [f"{e.summary} {e.why} {e.client}" for e in view.entries]
    )
    assert "evil.test" not in everything
    assert "http" not in everything
    assert top[0].summary == "Model summary 1. More at"  # the link is gone
    assert top[0].why == "Why story 1 matters to you, see"
    assert top[0].client == "Pitch it at"
    assert view.do_today == "Upgrade next in your client projects:"

    # The security watch: code, not a model, and the most urgent first.
    watch = view.section("security")
    assert [e.url for e in watch] == [
        "https://nvd.nist.gov/vuln/detail/CVE-2026-1111",  # exploited now
        "https://github.com/advisories/GHSA-lara-crit-0001",  # critical
        "https://github.com/advisories/GHSA-next-high-0001",  # high
    ]
    assert watch[1].watch == (
        "laravel/framework >= 11.0, < 11.9.2: critical severity, fixed in 11.9.2.",
    )
    assert watch[0].summary == "SQL injection."  # the advisory's own words
    assert all(not e.why for e in watch)

    # The article was read for its summary (robots.txt permitting), the rest weren't fetched.
    assert "Turbopack builds are now twice as fast" in writers.summaries[0]
    assert web.hits("css.test", "/robots.txt") == 1
    assert web.hits("css.test", "/anchor") == 0  # its robots.txt says no


async def test_public_models_never_see_your_profile(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    """Summaries go to a public model (it may train on what it's sent); notes to a private one."""
    tasks: dict[str, Any] = {
        task: {"privacy": "personal", "candidates": ["fake-local"]} for task in FAKE_TASKS
    }
    tasks["public_summarize"] = {"privacy": "public", "candidates": ["fake-public"]}
    models = parse_models_config(
        {
            "version": 1,
            "providers": {
                "fake": {"kind": "fake", "local": True, "trains_on_data": False},
                "fake-public": {"kind": "fake", "trains_on_data": True},
            },
            "models": {
                "fake-local": {"provider": "fake", "model": "fake"},
                "fake-public": {"provider": "fake-public", "model": "fake"},
            },
            "tasks": tasks,
        }
    )
    services = build_services(
        make_settings(), session_factory, clock=clock, embedder=HashEmbedder(), models_config=models
    )
    await with_profile(services)
    await store(services, *the_news())
    writers = Writers().install(services)

    brief = await builder(services, articles()).prepare(today(services))
    assert brief.models == {"summaries": "fake-public", "notes": "fake-local"}
    public = writers.sent["fake-public"]
    assert len(public) == 2  # eight stories, in batches of five
    for sent in public:
        for word in PRIVATE_WORDS:
            assert word not in sent, f"{word!r} reached the public model"
        assert '<untrusted nonce="' in sent  # the articles are marked as someone else's words
    # The private model does get to know you (so the test above can tell)...
    [notes] = writers.sent["fake-local"]
    assert "Breezy Digital" in notes
    assert "3 new retainers this quarter" in notes
    # ...but only what it needs: not your contacts or your boundaries.
    assert "Achieng" not in notes
    assert "pay anyone" not in notes


async def test_without_any_model_the_brief_still_goes_out(services: Services) -> None:
    await with_profile(services)
    await store(services, *the_news())
    Writers(down=True).install(services)
    build = builder(services, articles())

    brief = await build.prepare(today(services))
    assert brief.status == "ready"
    assert brief.models == {"summaries": None, "notes": None}
    view = build_view(brief, await entries_of(services, brief), decrypt=build.dec, source_names={})
    top = view.section("top")
    assert top[0].title == "Next.js 16.1"
    assert top[0].summary == "Faster builds with Turbopack."  # the source's own words
    assert not any(e.why for e in view.entries)
    assert view.do_today == (
        "Check your projects for laravel/framework >= 11.0, < 11.9.2 and update to 11.9.2."
    )
    assert view.script.startswith("Good morning. Here's your tech brief.")
    assert "Next.js 16.1" in view.script
    assert "On the security watch" in view.script


async def test_no_story_twice_and_mondays_brief_covers_the_weekend(
    services: Services, clock: FrozenClock
) -> None:
    assert clock.now().weekday() == 0  # the test clock starts on a Monday
    Writers().install(services)
    build = builder(services, articles())
    weekend = await store(
        services,
        {"source": "verge", "url": "https://www.theverge.com/sat", "title": "Saturday news",
         "hours_ago": 48},
        {"source": "verge", "url": "https://www.theverge.com/fri", "title": "Friday news",
         "hours_ago": 70},
        {"source": "verge", "url": "https://www.theverge.com/thu", "title": "Thursday news",
         "hours_ago": 90},
    )  # fmt: skip
    monday = await build.prepare(today(services))
    shown = {e.url for e in await entries_of(services, monday)}
    assert shown == {"https://www.theverge.com/sat", "https://www.theverge.com/fri"}
    assert "https://www.theverge.com/thu" in weekend  # stored, but older than 72 hours

    # Asking again changes nothing: the brief is built once.
    again = await build.prepare(today(services))
    assert again.id == monday.id
    assert len(await entries_of(services, again)) == 2

    async with transaction(services.session_factory) as session:
        row = await session.get(Brief, monday.id)
        assert row is not None
        row.status = "delivered"

    clock.advance(days=1)
    await store(
        services,
        {"source": "verge", "url": "https://www.theverge.com/tue", "title": "Tuesday news"},
        {"source": "verge", "url": "https://www.theverge.com/late", "title": "Late but new",
         "hours_ago": 26},  # published before Monday's brief, but only just read
    )  # fmt: skip
    tuesday = await build.prepare(today(services))
    assert tuesday.window_start == monday.window_end
    shown = {e.url for e in await entries_of(services, tuesday)}
    assert shown == {"https://www.theverge.com/tue", "https://www.theverge.com/late"}


async def test_a_brief_that_never_went_out_gives_its_stories_to_the_next(
    services: Services, clock: FrozenClock
) -> None:
    Writers().install(services)
    build = builder(services, articles())
    await store(
        services, {"source": "verge", "url": "https://www.theverge.com/mon", "title": "Monday"}
    )
    monday = await build.prepare(today(services))  # the PC was then off until Tuesday
    clock.advance(days=1)
    tuesday = await build.prepare(today(services))
    async with services.session_factory() as session:
        stale = await session.get(Brief, monday.id)
    assert stale is not None
    assert stale.status == "skipped"
    assert {e.url for e in await entries_of(services, tuesday)} == {"https://www.theverge.com/mon"}


async def test_a_quiet_morning_is_still_a_brief(services: Services) -> None:
    Writers().install(services)
    build = builder(services, articles())
    brief = await build.prepare(today(services))
    view = build_view(brief, [], decrypt=build.dec, source_names={})
    assert view.entries == ()
    assert "quiet morning" in view.script
    assert view.do_today == ""
