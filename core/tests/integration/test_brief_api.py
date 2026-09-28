"""The brief in the app: status, today's and past briefs, "make it now", votes and audio."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from jarvis.api.app import create_app
from jarvis.clock import FrozenClock
from jarvis.db.session import SessionFactory
from jarvis.memory.embeddings import HashEmbedder
from jarvis.services import Services, build_services
from tests.conftest import fake_models_config, make_settings
from tests.integration.api_helpers import ORIGIN, Harness, login, register
from tests.integration.brief_helpers import Newsroom, Speaker
from tests.webauthn_helpers import SoftAuthenticator

pytestmark = pytest.mark.db


@pytest.fixture
async def app_with_news(
    session_factory: SessionFactory, clock: FrozenClock, tmp_path: Path
) -> AsyncIterator[tuple[Harness, Newsroom]]:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "sources": [
                    {"id": "nextjs", "name": "Next.js", "kind": "feed", "category": "web",
                     "url": "https://nextjs.org/feed.xml"},
                    {"id": "techcabal", "name": "TechCabal", "kind": "feed",
                     "category": "africa", "url": "https://techcabal.com/feed/"},
                ],
            }
        )
    )  # fmt: skip
    settings = make_settings(
        JARVIS_ENABLE_SCHEDULER=False,
        JARVIS_SOURCES_FILE=str(sources),
        JARVIS_DATA_DIR=str(tmp_path / "data"),
    )
    news = Newsroom(clock)

    def factory(s: Any, sf: SessionFactory) -> Services:
        return build_services(
            s, sf, clock=clock, embedder=HashEmbedder(), models_config=fake_models_config()
        )

    app = create_app(
        settings,
        services_factory=factory,
        run_background=False,
        brief_fetcher=news.web.fetcher(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            yield (
                Harness(
                    app=app,
                    client=client,
                    state=app.state.jarvis,
                    authenticator=SoftAuthenticator(rp_id="localhost", origin=ORIGIN),
                    clock=clock,
                ),
                news,
            )


async def test_the_brief_in_the_app(app_with_news: tuple[Harness, Newsroom]) -> None:
    h, news = app_with_news
    assert (await h.client.get("/api/brief/status")).status_code == 401  # yours only
    await register(h)
    await login(h)
    brief = h.state.brief
    assert brief is not None
    brief._speech = Speaker  # a speech server for the audio version

    status = (await h.client.get("/api/brief/status")).json()
    assert status["enabled"] is True
    assert status["time"] == "07:00"
    assert status["today"]["status"] is None
    # It's Monday noon in Nairobi: today's 07:00 has passed, so the next is tomorrow's.
    assert status["next_delivery"] == "2026-01-06T07:00:00+03:00"
    assert status["streak"] == 0
    assert [s["id"] for s in status["sources"]] == ["nextjs", "techcabal"]
    assert (await h.client.get("/api/brief")).status_code == 404

    news.publish()
    made = await h.client.post("/api/brief/make")
    assert made.status_code == 202
    assert made.json() == {"making": True, "already": False}
    assert (await h.client.post("/api/brief/make")).json()["already"] is True
    assert brief._making is not None
    await brief._making

    today = (await h.client.get("/api/brief")).json()
    assert today["status"] == "delivered"  # made after 07:00: it goes out straight away
    assert today["on_time"] is False
    top = today["sections"]["top"]
    assert len(top) == 5
    shown = [item for section in today["sections"].values() for item in section]
    published = {s["link"] for entries in news.feeds.values() for s in entries}
    assert sorted(item["url"] for item in shown) == sorted(published)  # all 8, once each
    assert top[0]["reasons"]

    # Your 👍/👎.
    entry = top[0]["id"]
    voted = await h.client.post(f"/api/brief/entries/{entry}/vote", json={"vote": 1})
    assert voted.json() == {"id": entry, "vote": 1}
    cleared = await h.client.post(f"/api/brief/entries/{entry}/vote", json={"vote": None})
    assert cleared.json()["vote"] is None
    bad = await h.client.post(f"/api/brief/entries/{entry}/vote", json={"vote": 5})
    assert bad.status_code == 422
    missing = await h.client.post(
        "/api/brief/entries/00000000-0000-0000-0000-000000000000/vote", json={"vote": 1}
    )
    assert missing.status_code == 404

    # The audio version plays in the app, and nothing else on disk is reachable.
    assert today["audio_url"] == "/api/brief/audio/2026-01-05.mp3"
    audio = await h.client.get(today["audio_url"])
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/mpeg"
    assert audio.headers["content-disposition"].startswith("inline")
    for sneaky in ("..%2F..%2Fsecrets.mp3", "2026-01-04.mp3", "notes.txt"):
        assert (await h.client.get(f"/api/brief/audio/{sneaky}")).status_code == 404

    history = (await h.client.get("/api/brief/history")).json()
    assert [(d["day"], d["headline"]) for d in history] == [("2026-01-05", top[0]["title"])]
    status = (await h.client.get("/api/brief/status")).json()
    assert status["today"]["status"] == "delivered"
    assert status["today"]["deliveries"]["app"]["status"] == "sent"


async def test_a_broken_sources_file_turns_the_brief_off_not_jarvis(
    session_factory: SessionFactory, clock: FrozenClock, tmp_path: Path
) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text("version: 1\nsources:\n  - id: Bad Id\n    kind: feed\n")
    settings = make_settings(JARVIS_ENABLE_SCHEDULER=False, JARVIS_SOURCES_FILE=str(sources))

    def factory(s: Any, sf: SessionFactory) -> Services:
        return build_services(
            s, sf, clock=clock, embedder=HashEmbedder(), models_config=fake_models_config()
        )

    app = create_app(settings, services_factory=factory, run_background=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            h = Harness(
                app=app,
                client=client,
                state=app.state.jarvis,
                authenticator=SoftAuthenticator(rp_id="localhost", origin=ORIGIN),
                clock=clock,
            )
            await register(h)
            await login(h)
            status = (await client.get("/api/brief/status")).json()
            assert status["enabled"] is False
            assert "sources.yaml" in status["problem"]
            assert (await client.get("/api/brief")).status_code == 503
            assert (await client.get("/api/profile")).status_code == 200  # the rest runs
