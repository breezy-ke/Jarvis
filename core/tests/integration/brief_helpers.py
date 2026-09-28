"""Shared pieces for the brief tests: a newsroom, a speech server and push, on the test clock."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from jarvis.clock import FrozenClock
from jarvis.notify.push import PushResult
from jarvis.voice.speech import TTS_SAMPLE_RATE, SpeechError
from tests.fake_web import FakeWeb

NAIROBI = ZoneInfo("Africa/Nairobi")
MONDAY = date(2026, 1, 5)  # the test clock starts on this Monday, at noon in Nairobi


def at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=NAIROBI)


class Newsroom:
    """Two feeds that publish new stories whenever asked, each about something else."""

    def __init__(self, clock: FrozenClock) -> None:
        self.web = FakeWeb()
        self.clock = clock
        self.feeds: dict[str, list[dict[str, Any]]] = {
            "https://nextjs.org/feed.xml": [],
            "https://techcabal.com/feed/": [],
        }
        self.count = 0
        self.publish(web=0, africa=0)

    def _story(self, base: str) -> dict[str, Any]:
        self.count += 1
        n = self.count
        words = " ".join(f"topic{n}w{k}" for k in range(4))  # nothing in common with others
        return {
            "title": f"Story {n}: {words}",
            "link": f"{base}/story-{n}",
            "summary": f"Details {n}: {words} happened today.",
            "published": self.clock.now() - timedelta(minutes=n % 7 + 1),
        }

    def publish(self, *, web: int = 6, africa: int = 2) -> None:
        feeds = list(self.feeds)
        self.feeds[feeds[0]] += [self._story("https://nextjs.org/blog") for _ in range(web)]
        self.feeds[feeds[1]] += [self._story("https://techcabal.com") for _ in range(africa)]
        for url, entries in self.feeds.items():
            self.web.feed(url, entries[-40:])


class Speaker:
    """The speech server: a tone as long as the words would take (sped up for tests)."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.down = False

    async def synthesize(self, text: str, *, response_format: str = "mp3") -> bytes:
        assert response_format == "pcm"
        if self.down:
            raise SpeechError("speech server unreachable: ConnectError")
        self.said.append(text)
        samples = int(TTS_SAMPLE_RATE * max(0.6, len(text) / 150))
        tone = 6_000 * np.sin(np.arange(samples) / 8)
        return tone.astype("<i2").tobytes()


class Push:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.configured = True

    async def send(self, *, title: str, body: str, url: str | None = None) -> PushResult:
        self.sent.append((title, body, url))
        return PushResult(delivered=1, removed=0, configured=True)
