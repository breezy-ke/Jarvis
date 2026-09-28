"""Ranking, the security watch and the brief's renderings: the parts that are plain code."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from jarvis.brief.agents import Notes, Summaries, no_links
from jarvis.brief.build import (
    Plan,
    fallback_action,
    fallback_script,
    first_sentences,
    security_lines,
    urgency,
)
from jarvis.brief.rank import (
    SOURCE_RANGE,
    Scored,
    Taste,
    explain,
    interest_text,
    merge_same_stories,
    score,
    stack_terms,
)
from jarvis.brief.render import (
    BriefView,
    EntryView,
    email_html,
    email_subject,
    email_text,
    parse_vote,
    push_text,
    safe_url,
    telegram_messages,
    telegram_units,
    vote_data,
)
from jarvis.profile.schema import Profile

NOW = datetime(2026, 9, 28, 4, 0, tzinfo=UTC)
APP = "https://jarvis.test/brief"


@dataclass
class Item:
    """Just what ranking reads from a stored story."""

    title: str
    source_id: str = "web"
    summary: str = ""
    url: str = "https://example.test/story"
    kind: str = "story"
    published_at: datetime = NOW
    extra: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


def rank(item: Item, **options: Any) -> Scored:
    values: dict[str, Any] = {
        "now": NOW,
        "source_weight": 1.0,
        "interest": None,
        "stacks": [],
        "taste": Taste(),
    }
    values.update(options)
    return score(item, **values)


# --- Ranking -----------------------------------------------------------------------------------


def test_a_story_counts_half_as_much_every_18_hours() -> None:
    fresh = rank(Item("A"))
    assert rank(Item("B", published_at=NOW - timedelta(hours=18))).score == pytest.approx(
        fresh.score / 2
    )
    assert rank(Item("C", published_at=NOW - timedelta(hours=36))).score == pytest.approx(
        fresh.score / 4
    )
    # A source whose clock runs ahead doesn't jump the queue.
    assert rank(Item("D", published_at=NOW + timedelta(hours=3))).score == pytest.approx(
        fresh.score
    )


def test_your_stacks_hacker_news_and_your_work_lift_a_story() -> None:
    stacks = ["Next.js", "Laravel"]
    plain = rank(Item("A new CSS feature"), stacks=stacks)
    named = rank(Item("Next.js 16.1 is out"), stacks=stacks)
    assert named.score == pytest.approx(plain.score * 1.3)
    assert named.why["stack"] == ["Next.js"]
    assert "stack" not in rank(Item("Nextgen phones"), stacks=["Next"]).why  # a word, not a part
    popular = rank(Item("A new CSS feature", extra={"hn": {"points": 999}}), stacks=stacks)
    assert popular.score > plain.score
    assert popular.why["points"] == 999
    close = rank(Item("x", embedding=[1.0, 0.0]), interest=[1.0, 0.0])
    far = rank(Item("y", embedding=[0.0, 1.0]), interest=[1.0, 0.0])
    assert close.score == pytest.approx(far.score * 3)  # (0.5 + 1) against (0.5 + 0)
    assert rank(Item("z"), source_weight=2.0).score == pytest.approx(plain.score * 2)


def test_your_votes_move_a_source_between_half_and_double() -> None:
    taste = Taste.from_votes(
        [("verge", -1, None)] * 5
        + [("hn", 1, None)] * 9
        + [("web", 1, None), ("web", -1, None), ("ars", 1, None)]
    )
    assert taste.sources["verge"] == SOURCE_RANGE[0]
    assert taste.sources["hn"] == SOURCE_RANGE[1]
    assert taste.sources["web"] == 1.0
    assert taste.sources["ars"] == 2.0
    liked = rank(Item("x", source_id="hn"), taste=taste)
    disliked = rank(Item("x", source_id="verge"), taste=taste)
    assert liked.score == pytest.approx(disliked.score * 4)
    assert liked.why["source_votes"] == 2.0


def test_stories_like_ones_you_liked_rise_and_ones_like_you_disliked_fall() -> None:
    taste = Taste.from_votes([("a", 1, [1.0, 0.0]), ("b", -1, [0.0, 1.0])])
    like = rank(Item("x", source_id="c", embedding=[1.0, 0.0]), taste=taste)
    dislike = rank(Item("y", source_id="c", embedding=[0.0, 1.0]), taste=taste)
    neutral = rank(Item("z", source_id="c"), taste=taste)
    assert like.score > neutral.score > dislike.score
    assert like.why.get("liked_similar") is True
    assert dislike.why.get("disliked_similar") is True


def test_the_same_story_from_two_places_is_shown_once() -> None:
    best = Scored(Item("Story", source_id="verge", embedding=[1.0, 0.0]), score=2.0, why={})
    copy = Scored(Item("Story, again", source_id="tc", embedding=[0.99, 0.02]), score=1.0, why={})
    other = Scored(Item("Other", source_id="ars", embedding=[0.0, 1.0]), score=1.5, why={})
    kept = merge_same_stories([copy, other, best])
    assert [k.item.title for k in kept] == ["Story", "Other"]
    assert kept[0].also == ["tc"]


def test_why_this_in_plain_words() -> None:
    why = {
        "stack": ["Next.js"],
        "liked_similar": True,
        "relevance": 0.5,
        "points": 420,
        "source_votes": 1.5,
        "also": ["verge"],
    }
    assert explain(why, {"verge": "The Verge"}) == [
        "Mentions Next.js",
        "Like stories you gave a 👍",
        "Close to your work",
        "420 points on Hacker News",
        "From a source you rate highly",
        "Also in The Verge",
    ]
    assert explain({"disliked_similar": True}, {}) == ["Though similar to stories you gave a 👎"]
    assert explain({}, {}) == ["Fresh from a source you follow"]


def test_what_you_care_about_comes_from_your_profile() -> None:
    profile = Profile.model_validate(
        {
            "engineering": {
                "primary_stacks": ["Next.js", " Laravel "],
                "also_uses": ["Tailwind CSS", "x"],
                "hosting": ["Cloudflare"],
            },
            "business": {"services": ["Web apps"]},
            "clients": {"ideal_clients": [{"play": "ea_smes", "description": "Nairobi SMEs"}]},
            "goals": {"business_goals": ["3 retainers"], "learning_goals": [" "]},
        }
    )
    assert interest_text(profile) == (
        "Next.js. Laravel. Tailwind CSS. x. Cloudflare. Web apps. Nairobi SMEs. 3 retainers"
    )
    assert stack_terms(profile) == ["Laravel", "Next.js", "Tailwind CSS"]  # "x" is too short
    assert interest_text(Profile()) == ""


# --- The security watch and the fallbacks --------------------------------------------------------


def advisory(severity: str, published: datetime, *, patched: str = "16.0.4") -> Scored:
    data = {
        "severity": severity,
        "packages": [
            {"ecosystem": "npm", "name": "next", "vulnerable": "< 16.0.4", "patched": patched}
        ],
    }
    item = Item(
        f"next: {severity}", kind="advisory", published_at=published, extra={"advisory": data}
    )
    return Scored(item, 1.0, {})


EXPLOITED = Scored(
    Item(
        "WordPress: SQL injection",
        kind="exploited",
        extra={
            "exploited": {
                "cve": "CVE-2026-1111",
                "product": "WordPress",
                "action": "Update to 6.8.3.",
            }
        },
    ),
    1.0,
    {},
)


def test_the_security_watch_puts_exploited_first_then_severity() -> None:
    older = advisory("critical", NOW - timedelta(hours=5))
    newer = advisory("critical", NOW)
    high = advisory("high", NOW)
    assert sorted([high, older, EXPLOITED, newer], key=urgency) == [EXPLOITED, newer, older, high]
    assert security_lines([EXPLOITED, advisory("high", NOW, patched="")]) == [
        "WordPress: CVE-2026-1111 is being exploited now. Update to 6.8.3.",
        "next < 16.0.4: high severity, no fix yet.",
    ]


def test_without_a_model_the_brief_still_says_something_useful() -> None:
    top = Scored(Item("Next.js 16.1 is out", source_id="nextjs"), 1.0, {})
    plan = Plan(top=[top], security=[advisory("high", NOW)])
    assert fallback_action(plan) == "Check your projects for next < 16.0.4 and update to 16.0.4."
    assert fallback_action(Plan(top=[top])) == "Read “Next.js 16.1 is out”."
    assert fallback_action(Plan()) == ""
    names = {"nextjs": "the Next.js blog"}
    script = fallback_script(
        plan, {top.item.id: "It builds faster, see https://evil.test/x"}, names.get
    )
    assert script.startswith("Good morning. Here's your tech brief.")
    assert "Number 1, from the Next.js blog: Next.js 16.1 is out. It builds faster, see" in script
    assert "On the security watch. next < 16.0.4: high severity, fixed in 16.0.4." in script
    assert "http" not in script
    assert "evil.test" not in script
    assert "quiet morning" in fallback_script(Plan(), {}, names.get)
    assert first_sentences("One. Two! Three? Four.") == "One. Two!"
    assert len(first_sentences("word " * 200)) == 400


def test_models_cannot_put_links_in_the_brief() -> None:
    assert no_links("See https://evil.test/a?b=c and www.evil.test now") == "See and now"
    summaries = Summaries.model_validate(
        {"items": [{"index": 1, "summary": "Read http://x.test/y today."}]}
    )
    assert summaries.items[0].summary == "Read today."
    notes = Notes.model_validate(
        {
            "items": [{"index": 1, "why": "Go to www.phish.test", "client": "HTTPS://A.TEST"}],
            "do_today": "Visit https://a.test first",
            "script": "Say hi. http://b.test",
        }
    )
    assert (notes.items[0].why, notes.items[0].client) == ("Go to", "")
    assert (notes.do_today, notes.script) == ("Visit first", "Say hi.")


# --- Renderings ----------------------------------------------------------------------------------


def entry(section: str, rank_: int, title: str, url: str, **options: Any) -> EntryView:
    values: dict[str, Any] = {"source": "Source", "summary": ""}
    values.update(options)
    return EntryView(id=uuid.uuid4(), section=section, rank=rank_, title=title, url=url, **values)


def a_brief(*entries: EntryView, do_today: str = "Update next in the clinic site.") -> BriefView:
    chosen = entries or (
        entry(
            "top",
            1,
            "Next.js 16.1 <script>alert(1)</script>",
            "https://nextjs.org/blog/next-16-1",
            source="Next.js",
            summary="Faster builds & more.",
            why="Your clinic site runs Next.js.",
            client="Offer the upgrade to Nairobi SMEs.",
            reasons=("Mentions Next.js",),
        ),
        entry("top", 2, "A tiny database", "https://tiny.test/db?id=1&x=2", source="Hacker News"),
        entry(
            "security",
            1,
            "next: file read",
            "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
            watch=("next < 16.0.4: high severity, fixed in 16.0.4.",),
        ),
        entry(
            "africa", 1, "M-Pesa opens its API", "https://techcabal.com/mpesa", source="TechCabal"
        ),
        entry("quick", 1, "acme/rocket", "https://github.com/acme/rocket", source="GitHub"),
    )
    return BriefView(
        id=uuid.uuid4(),
        day=date(2026, 9, 28),
        status="ready",
        scheduled_for=None,
        delivered_at=None,
        on_time=None,
        do_today=do_today,
        script="Good morning.",
        entries=tuple(chosen),
    )


def hrefs(markup: str) -> set[str]:
    return {link.replace("&amp;", "&") for link in re.findall(r'href="([^"]+)"', markup)}


def test_every_rendering_links_each_story_to_its_source_and_nothing_else() -> None:
    brief = a_brief()
    [(telegram, buttons)] = telegram_messages(brief, app_link=APP)
    page = email_html(brief, app_link=APP)
    text = email_text(brief, app_link=APP)
    sources = {e.url for e in brief.entries}
    assert hrefs(telegram) == sources | {APP}
    assert hrefs(page) == sources | {APP}
    assert all(url in text for url in sources)
    for markup in (telegram, page):
        assert "<script>" not in markup
        assert "&lt;script&gt;" in markup
    assert "Why it matters: Your clinic site runs Next.js." in text
    assert "💡 Your clinic site runs Next.js." in telegram
    assert email_subject(brief) == "Your tech brief, Monday 28 September"
    assert "Monday 28 September" in telegram
    # 👍/👎 for the top stories only.
    top = brief.section("top")
    assert buttons == [
        [
            {"text": f"{e.rank} 👍", "callback_data": vote_data(e.id, 1)},
            {"text": f"{e.rank} 👎", "callback_data": vote_data(e.id, -1)},
        ]
        for e in top
    ]


def test_a_link_that_isnt_http_is_shown_as_text() -> None:
    assert safe_url("https://a.test/ok") == "https://a.test/ok"
    for bad in ("javascript:alert(1)", "data:text/html,hi", "https://a.test/x y", "https://", ""):
        assert safe_url(bad) is None, bad
    brief = a_brief(entry("top", 1, "Sneaky", "javascript:alert(1)"))
    [(telegram, _)] = telegram_messages(brief)
    assert "javascript" not in telegram
    assert "Sneaky" in telegram
    assert "javascript" not in email_html(brief)


def test_a_quiet_morning_says_so() -> None:
    brief = a_brief(entry("quick", 1, "Only this", "https://q.test/"), do_today="")
    [(telegram, buttons)] = telegram_messages(brief)
    assert "Nothing urgent today." in telegram
    assert "Nothing urgent today." in email_text(brief)
    assert buttons == []
    assert push_text(brief) == ("Your tech brief", "A quiet morning: nothing new worth your time.")
    assert push_text(a_brief()) == ("Your tech brief", "Next.js 16.1 <script>alert(1)</script>")


def test_a_long_brief_is_split_into_messages_telegram_accepts() -> None:
    rocket = "🚀" * 150  # counts double for Telegram
    stories = [
        entry(
            "top",
            n,
            f"{rocket} {n}",
            f"https://long.test/{n}",
            summary="s" * 450,
            why="w" * 300,
            client="c" * 300,
        )
        for n in range(1, 6)
    ]
    watch = [
        entry(
            "security",
            n,
            f"advisory {n}",
            f"https://github.com/advisories/{n}",
            watch=("x" * 300, "y" * 300),
        )
        for n in range(1, 6)
    ]
    brief = a_brief(*stories, *watch)
    messages = telegram_messages(brief, app_link=APP)
    assert len(messages) > 1
    seen: list[str] = []
    for markup, buttons in messages:
        assert telegram_units(markup) <= 4_096
        seen += sorted(hrefs(markup))
        for row in buttons:  # a story's buttons travel with it
            vote = parse_vote(row[0]["callback_data"])
            assert vote is not None
            [voted] = [e for e in brief.entries if e.id == vote[0]]
            assert voted.url in hrefs(markup)
    assert sorted(seen) == sorted([*(e.url for e in brief.entries), APP])  # each exactly once


def test_vote_buttons_show_your_vote_and_carry_only_what_they_should() -> None:
    liked = entry("top", 1, "Liked", "https://a.test/", vote=1)
    [(_, [row])] = telegram_messages(a_brief(liked))
    assert [b["text"] for b in row] == ["1 👍 ✓", "1 👎"]
    assert parse_vote(vote_data(liked.id, -1)) == (liked.id, -1)
    assert parse_vote(vote_data(liked.id, 1)) == (liked.id, 1)
    for junk in (f"ap:{liked.id}:0123456789abcdef", f"bv:{liked.id}:x", "bv:" + "-" * 36 + ":u"):
        assert parse_vote(junk) is None, junk
    assert len(vote_data(liked.id, 1).encode()) <= 64  # Telegram's limit for button data
