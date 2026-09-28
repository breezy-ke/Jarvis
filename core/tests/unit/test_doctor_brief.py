"""`make doctor` on the tech brief and research: settings, delivery, streak and sources."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import respx

from jarvis import doctor
from jarvis.brief.config import BriefConfig, load_sources_config, parse_sources_config
from jarvis.config import REPO_ROOT, Settings
from tests.fake_web import FakeWeb

REPO_CONFIG = REPO_ROOT / "config"
TODAY = date(2026, 9, 28)
SOURCES = load_sources_config(REPO_CONFIG / "sources.yaml")


def brief(
    day: date, status: str = "delivered", on_time: bool | None = True, **deliveries: Any
) -> Any:
    return SimpleNamespace(day=day, status=status, on_time=on_time, deliveries=deliveries)


def run(**options: Any) -> dict[str, doctor.Check]:
    values: dict[str, Any] = {
        "config": BriefConfig(),
        "sources": SOURCES,
        "latest": brief(TODAY),
        "streak": 3,
        "states": {s.id: SimpleNamespace(failures=0, last_error=None) for s in SOURCES.sources},
        "today": TODAY,
        "profile_time": None,
    }
    values.update(options)
    return {c.title: c for c in doctor.brief_checks(**values)}


def test_the_shipped_brief_settings_pass() -> None:
    checks, config, sources = doctor.check_brief_config(Settings(JARVIS_CONFIG_DIR=REPO_CONFIG))
    assert config is not None
    assert sources is not None
    assert [(c.status, c.title) for c in checks] == [
        (doctor.OK, "brief.yaml"),
        (doctor.OK, "sources.yaml"),
    ]
    assert checks[0].detail == (
        "every morning at 07:00: the app, a notification, Telegram, a copy in your inbox, "
        "an audio version"
    )
    assert checks[1].detail == f"{len(SOURCES.sources)} of {len(SOURCES.sources)} sources on"


def test_broken_brief_settings_say_the_brief_is_off(tmp_path: Path) -> None:
    broken = tmp_path / "sources.yaml"
    broken.write_text("version: 1\nsources:\n  - id: x\n    kind: nope\n")
    checks, config, sources = doctor.check_brief_config(
        Settings(JARVIS_CONFIG_DIR=REPO_CONFIG, JARVIS_SOURCES_FILE=broken)
    )
    assert config is not None
    assert sources is None
    assert checks[-1].status == doctor.FAIL
    assert "the brief is off" in checks[-1].fix


def test_the_doctor_follows_the_brief_day_by_day() -> None:
    fine = run()
    assert (fine["Last brief"].status, fine["Last brief"].detail) == (
        doctor.OK,
        "Mon 28 Sep: on time",
    )
    assert fine["On-time streak"].detail == "3 days; 7 in a row is the goal"
    assert run(streak=7)["On-time streak"].detail == "7 days; the 7-day goal is met"
    assert "On-time streak" not in run(streak=0)
    assert run(latest=None)["Last brief"].detail.startswith("none yet: the first arrives at 07:00")

    late = run(latest=brief(TODAY, on_time=False))["Last brief"]
    assert late.status == doctor.WARN
    assert "asleep at 07:00" in late.fix
    skipped = run(latest=brief(TODAY, status="skipped", on_time=None))["Last brief"]
    assert "Jarvis was off" in skipped.detail
    stale = run(latest=brief(date(2026, 9, 25)))["Last brief"]
    assert (stale.status, stale.detail) == (doctor.WARN, "Fri 25 Sep, and none since")
    ready = run(latest=brief(TODAY, status="ready", on_time=None))["Last brief"]
    assert ready.detail == "Mon 28 Sep: ready, going out at 07:00"

    failing = run(
        latest=brief(
            TODAY,
            telegram={"status": "failed", "error": "TelegramError: unreachable"},
            inbox={"status": "sent"},
        )
    )["Brief delivery"]
    assert failing.detail == "not yet to telegram (TelegramError: unreachable)"


def test_the_doctor_names_failing_sources_and_a_mismatched_time() -> None:
    states = {s.id: SimpleNamespace(failures=0, last_error=None) for s in SOURCES.sources}
    states["techcabal"] = SimpleNamespace(failures=4, last_error="techcabal.com answered 503")
    sources = run(states=states)["Brief sources"]
    assert sources.status == doctor.WARN
    assert "TechCabal (techcabal.com answered 503)" in sources.detail
    assert run(states={})["Brief sources"].detail == "not read yet"

    for said, clock in (("6:30am", "06:30"), ("06:30", "06:30"), ("7pm", "19:00")):
        mismatch = run(profile_time=said)["Brief time"]
        assert mismatch.status == doctor.WARN
        assert f'`time: "{clock}"`' in mismatch.fix
    assert "Brief time" not in run(profile_time="7am")
    assert "Brief time" not in run(profile_time="whenever suits")


@respx.mock
async def test_the_doctor_checks_searxng() -> None:
    settings = Settings(JARVIS_SEARXNG_URL="http://searxng:8080")
    respx.get("http://searxng:8080/healthz").mock(return_value=httpx.Response(200, text="OK"))
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(403, text="format not allowed")
    )
    running = await doctor.check_research(settings, online=False)
    assert (running.status, running.detail) == (doctor.OK, "running")
    no_json = await doctor.check_research(settings, online=True)
    assert no_json.status == doctor.WARN
    assert "answered 403" in no_json.detail

    respx.get("http://searxng:8080/healthz").mock(side_effect=httpx.ConnectError("refused"))
    down = await doctor.check_research(settings, online=False)
    assert down.status == doctor.WARN
    assert "`make up`" in down.fix


async def test_online_every_source_is_asked_once() -> None:
    web = FakeWeb()
    web.page("https://nextjs.org/feed.xml", "<rss/>", content_type="application/rss+xml")
    web.page("https://hacker-news.firebaseio.com/v0/topstories.json", "[]")
    sources = parse_sources_config(
        {
            "version": 1,
            "sources": [
                {"id": "nextjs", "name": "Next.js", "kind": "feed", "category": "web",
                 "url": "https://nextjs.org/feed.xml"},
                {"id": "hn", "name": "Hacker News", "kind": "hn", "category": "news"},
                {"id": "gone", "name": "Gone", "kind": "feed", "category": "news",
                 "url": "https://gone.test/feed"},
                {"id": "off", "name": "Off", "kind": "feed", "category": "news",
                 "url": "https://off.test/feed", "enabled": False},
            ],
        }
    )  # fmt: skip
    check = await doctor.check_sources_online(Settings(), sources, fetcher=web.fetcher())
    assert check.status == doctor.WARN
    assert check.detail == "1 of 3 didn't answer: Gone: gone.test answered 404"
    assert web.hits("off.test") == 0
