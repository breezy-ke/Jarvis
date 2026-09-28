"""Your brief at 07:00: on every channel once, on time, retried when a channel fails,
caught up the same day after a morning offline, and skipped once its day has passed."""

from __future__ import annotations

import base64
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes, policy
from pathlib import Path

import pytest
import soundfile
from sqlalchemy import select

from jarvis.brief.config import BriefConfig, parse_sources_config
from jarvis.brief.render import parse_vote
from jarvis.brief.service import BriefService, spoken_parts
from jarvis.chat.service import ChatService
from jarvis.clock import FrozenClock
from jarvis.db.models import Brief, BriefEntry, MailMessage, MailThread
from jarvis.db.session import transaction
from jarvis.mail.mime import BRIEF_HEADER, brief_mark, is_brief_mark
from jarvis.mail.service import MailService
from jarvis.security.crypto import Vault
from jarvis.services import Services
from jarvis.telegram.bot import TelegramBot
from jarvis.voice.speech import TTS_SAMPLE_RATE
from tests.integration.brief_helpers import MONDAY, Newsroom, Push, Speaker, at
from tests.integration.fakes import AMANI, FakeTelegram, Updates
from tests.integration.mail_helpers import OWNER, MailRig
from tests.profile_helpers import FULL_PROFILE

pytestmark = pytest.mark.db

SOURCES = parse_sources_config(
    {
        "version": 1,
        "sources": [
            {"id": "nextjs", "name": "Next.js", "kind": "feed", "category": "web",
             "url": "https://nextjs.org/feed.xml"},
            {"id": "techcabal", "name": "TechCabal", "kind": "feed", "category": "africa",
             "url": "https://techcabal.com/feed/"},
        ],
    }
)  # fmt: skip


class Morning:
    """Everything the brief touches, wired the way Jarvis wires it."""

    def __init__(
        self,
        services: Services,
        clock: FrozenClock,
        mailer: MailService,
        rig: MailRig,
        audio_dir: Path,
    ) -> None:
        self.services = services
        self.clock = clock
        self.rig = rig
        self.news = Newsroom(clock)
        self.speaker = Speaker()
        self.push = Push()
        services.push = self.push  # type: ignore[assignment]
        self.telegram = FakeTelegram()
        self.updates = Updates()
        self.bot = TelegramBot(
            services=services,
            chat=ChatService(services),
            api=self.telegram,  # type: ignore[arg-type]
        )
        self.brief = BriefService(
            services,
            config=BriefConfig(),
            sources=SOURCES,
            fetcher=self.news.web.fetcher(),
            mail=mailer,
            speech=lambda: self.speaker,
            audio_dir=audio_dir,
        )
        self.brief.telegram = self.bot
        self.bot.brief = self.brief

    async def start(self) -> None:
        async with transaction(self.services.session_factory) as session:
            await self.services.profiles.update(session, FULL_PROFILE, created_by="owner")
        await self.bot.connect()
        async with transaction(self.services.session_factory) as session:
            code, _ = await self.bot.create_pairing_link(session)
        await self.bot.handle_update(self.updates.message(f"/start {code.compact}"))
        self.telegram.sent.clear()  # the welcome

    async def run_until(self, until: datetime) -> None:
        """Jarvis's loop, on the test clock: wake when it says, do what's due."""
        while self.clock.now() < until:
            self.clock.advance(
                seconds=min(
                    self.brief.seconds_to_next(), (until - self.clock.now()).total_seconds()
                )
            )
            await self.brief.tick(read=True)

    async def brief_on(self, day: date) -> Brief | None:
        return await self.brief.brief_for(day)

    def briefs_on_telegram(self) -> list[str]:
        return [s.text for s in self.telegram.sent if "Your tech brief" in s.text]


@pytest.fixture
async def morning(
    services: Services, clock: FrozenClock, mailer: MailService, mail: MailRig, tmp_path: Path
) -> Morning:
    m = Morning(services, clock, mailer, mail, tmp_path / "briefs")
    await m.start()
    return m


async def test_the_brief_arrives_at_seven_on_every_channel_once(morning: Morning) -> None:
    tuesday = MONDAY + timedelta(days=1)
    morning.clock.set(at(tuesday, 5, 30))
    morning.news.publish()
    await morning.run_until(at(tuesday, 6, 39, 59))
    assert await morning.brief_on(tuesday) is None  # read, but not put together yet

    await morning.run_until(at(tuesday, 6, 59, 45))
    ready = await morning.brief_on(tuesday)
    assert ready is not None
    assert ready.status == "ready"
    assert morning.briefs_on_telegram() == []  # nothing goes out before 07:00
    assert morning.brief.seconds_to_next() == 15  # it wakes right on time

    await morning.run_until(at(tuesday, 7, 0, 0))
    brief = await morning.brief_on(tuesday)
    assert brief is not None
    assert brief.status == "delivered"
    assert brief.delivered_at == at(tuesday, 7).astimezone(UTC)
    assert brief.on_time is True
    deliveries = brief.deliveries
    assert {c: d["status"] for c, d in deliveries.items()} == {
        "app": "sent",
        "audio": "sent",
        "push": "sent",
        "telegram": "sent",
        "voice": "sent",
        "inbox": "sent",
    }

    # The push: your top headline.
    [(title, body, url)] = morning.push.sent
    view = await morning.brief.view(day=tuesday)
    assert (title, body, url) == ("Your tech brief", view.headline, "/brief")

    # Telegram: the brief with 👍/👎 for the top stories, one sound, then the voice note.
    brief_messages = [s for s in morning.telegram.sent if s.html]
    assert brief_messages[0].text.startswith("☕ <b>Your tech brief</b> · Tuesday 6 January")
    assert brief_messages[0].silent is False
    assert all(m.silent for m in brief_messages[1:])
    votes = [parse_vote(row[0]["callback_data"]) for m in brief_messages for row in m.buttons or []]
    assert [v[0] for v in votes if v] == [e.id for e in view.section("top")]
    for entry in view.entries:  # every item links to its source
        assert any(f'href="{entry.url}"' in m.text for m in brief_messages)
    [note] = morning.telegram.voice_notes
    assert note["caption"].startswith("🎧 Your tech brief, read aloud")
    assert note["silent"] is True

    # The audio version: a real MP3 of the script, kept with the brief.
    assert brief.audio_file == "2026-01-06.mp3"
    path = morning.brief.audio_path(brief.audio_file)
    assert path is not None
    assert note["audio"] == path.read_bytes()
    samples, rate = soundfile.read(io.BytesIO(path.read_bytes()))
    assert rate == TTS_SAMPLE_RATE
    assert abs(len(samples) / rate - (brief.audio_seconds or 0)) < 1.5
    assert " ".join(morning.speaker.said) == " ".join(spoken_parts(view.script))

    # Your inbox: a copy placed there, labelled, from Jarvis to you. Nothing was sent.
    fake = morning.rig.fake
    [copy] = fake.inserted()
    assert fake.sent() == []
    label = next(i for i, name in fake.labels.items() if name == "Jarvis/Brief")
    assert copy["labelIds"] == ["INBOX", "UNREAD", label]
    email = message_from_bytes(copy["raw"], policy=policy.default)
    assert email["Subject"] == "Your tech brief, Tuesday 6 January"
    assert email["From"] == f"Jarvis <{OWNER}>"
    assert email["To"] == OWNER
    assert is_brief_mark(morning.services.vault, str(email[BRIEF_HEADER]))
    html_part = email.get_body(("html",))
    text_part = email.get_body(("plain",))
    assert html_part is not None
    assert text_part is not None
    for entry in view.entries:
        assert f'href="{entry.url}"' in str(html_part.get_content())
        assert entry.url in str(text_part.get_content())

    # Later ticks send nothing twice.
    await morning.run_until(at(tuesday, 9, 0))
    assert len(morning.briefs_on_telegram()) == 1
    assert len(morning.telegram.voice_notes) == 1
    assert len(fake.inserted()) == 1
    assert len(morning.push.sent) == 1


async def test_the_inbox_copy_is_not_mail_to_sort_but_a_fake_one_is_suspicious(
    morning: Morning, mailer: MailService
) -> None:
    tuesday = MONDAY + timedelta(days=1)
    morning.clock.set(at(tuesday, 6, 30))
    morning.news.publish()
    await morning.run_until(at(tuesday, 7, 0))
    [copy] = morning.rig.fake.inserted()
    forged = morning.rig.fake.deliver(
        sender=f"Jarvis <{OWNER}>",
        subject="Your tech brief, Tuesday 6 January",
        body="Urgent: sign in again at the link below.",
        headers={BRIEF_HEADER: "gAAAAABforged-but-looks-the-part"},
    )
    real = morning.rig.fake.deliver()

    await mailer.sync_round()
    async with morning.services.session_factory() as session:
        stored = {m.id for m in await session.scalars(select(MailMessage))}
    assert copy["id"] not in stored  # Jarvis's own copy: never sorted, alerted on or drafted
    assert {forged, real} <= stored

    await mailer.triage_round()
    async with morning.services.session_factory() as session:
        forged_thread = await session.get(MailThread, forged)
    assert forged_thread is not None
    assert "fake_jarvis" in forged_thread.signals
    assert forged_thread.category == "suspicious"


async def test_a_channel_that_fails_is_tried_again_and_nothing_goes_twice(
    morning: Morning,
) -> None:
    tuesday = MONDAY + timedelta(days=1)
    morning.clock.set(at(tuesday, 6, 30))
    morning.news.publish()
    morning.telegram.fail_next = 1  # Telegram is unreachable at 07:00
    morning.rig.fake.faults.add("insert_lost")  # the copy lands, but Gmail's answer is lost
    morning.speaker.down = True  # and the speech server is still starting

    await morning.run_until(at(tuesday, 7, 0))
    brief = await morning.brief_on(tuesday)
    assert brief is not None
    assert brief.on_time is True  # it's in the app and on your phone at 07:00
    status = {c: d["status"] for c, d in brief.deliveries.items()}
    assert status == {
        "app": "sent",
        "audio": "failed",
        "push": "sent",
        "telegram": "failed",
        "voice": "failed",
        "inbox": "failed",
    }
    assert "unreachable" in brief.deliveries["audio"]["error"]
    assert len(morning.rig.fake.inserted()) == 1

    await morning.run_until(at(tuesday, 7, 2, 30))  # tried again two minutes later
    brief = await morning.brief_on(tuesday)
    assert brief is not None
    status = {c: d["status"] for c, d in brief.deliveries.items()}
    assert (status["telegram"], status["inbox"], status["audio"]) == ("sent", "sent", "failed")
    assert status["voice"] == "failed"  # waiting for the audio

    morning.speaker.down = False
    await morning.run_until(at(tuesday, 7, 31))
    brief = await morning.brief_on(tuesday)
    assert brief is not None
    assert brief.deliveries["audio"]["status"] == "failed"  # half-hourly now: not yet
    await morning.run_until(at(tuesday, 7, 33))
    brief = await morning.brief_on(tuesday)
    assert brief is not None
    assert {d["status"] for d in brief.deliveries.values()} == {"sent"}
    assert len(morning.briefs_on_telegram()) == 1
    assert len(morning.telegram.voice_notes) == 1
    assert len(morning.rig.fake.inserted()) == 1  # found where it landed, not placed twice
    assert brief.deliveries["inbox"]["id"] == morning.rig.fake.inserted()[0]["id"]
    assert len(morning.push.sent) == 1


async def test_a_morning_offline_is_caught_up_that_day_and_a_missed_day_is_skipped(
    morning: Morning,
) -> None:
    tuesday, wednesday, thursday = (MONDAY + timedelta(days=n) for n in (1, 2, 3))
    # Tuesday: the PC is off until 22:00. The brief goes out then, quietly.
    morning.clock.set(at(tuesday, 5, 0))
    morning.news.publish()
    morning.clock.set(at(tuesday, 22, 0))
    await morning.brief.tick(read=True)
    late = await morning.brief_on(tuesday)
    assert late is not None
    assert late.status == "delivered"
    assert late.on_time is False
    assert late.deliveries["push"] == {
        "at": late.deliveries["push"]["at"],
        "status": "skipped",
        "detail": "Quiet hours",
    }
    [message, *_] = [s for s in morning.telegram.sent if s.html]
    assert message.silent is True  # no sound at night

    # Wednesday: prepared at 06:40, then the power went before 07:00, until Thursday.
    morning.clock.set(at(wednesday, 6, 30))
    morning.news.publish()
    await morning.run_until(at(wednesday, 6, 45))
    stale = await morning.brief_on(wednesday)
    assert stale is not None
    assert stale.status == "ready"

    morning.clock.set(at(thursday, 9, 15))
    morning.news.publish()
    await morning.brief.tick(read=True)
    stale = await morning.brief_on(wednesday)
    assert stale is not None
    assert stale.status == "skipped"  # yesterday's news isn't sent today
    thursday_brief = await morning.brief_on(thursday)
    assert thursday_brief is not None
    assert thursday_brief.status == "delivered"
    assert thursday_brief.on_time is False
    # Its stories include Wednesday's, which you never got.
    async with morning.services.session_factory() as session:
        titles = {
            e.title
            for e in await session.scalars(
                select(BriefEntry).where(BriefEntry.brief_id == thursday_brief.id)
            )
        }
    assert len(titles) > 8
    assert len(morning.briefs_on_telegram()) == 2  # Tuesday's and Thursday's only
    assert await morning.brief.streak() == 0


async def test_a_brief_finished_after_midnight_waits_for_nobody(morning: Morning) -> None:
    """Made for Tuesday but only ready after midnight: Tuesday's news isn't sent on Wednesday."""
    tuesday = MONDAY + timedelta(days=1)
    morning.clock.set(at(tuesday, 23, 58))
    morning.news.publish()
    await morning.brief.builder.prepare(tuesday)
    morning.clock.set(at(tuesday + timedelta(days=1), 0, 1))
    brief = await morning.brief.deliver(tuesday)
    assert brief is not None
    assert brief.status == "skipped"
    assert brief.deliveries == {}
    assert morning.briefs_on_telegram() == []
    assert morning.rig.fake.inserted() == []


async def test_seven_mornings_in_a_row_on_time(morning: Morning) -> None:
    """The exit test: a week of briefs, each at 07:00 sharp, nothing repeated."""
    first = MONDAY + timedelta(days=1)
    seen: set[str] = set()
    for n in range(7):
        day = first + timedelta(days=n)
        morning.clock.set(at(day, 6, 20))
        morning.news.publish()
        await morning.run_until(at(day, 7, 5))
        brief = await morning.brief_on(day)
        assert brief is not None, day
        assert brief.on_time is True, day
        assert brief.delivered_at == at(day, 7).astimezone(UTC), day
        assert brief.window_end <= at(day, 6, 40).astimezone(UTC) + timedelta(minutes=1)
        async with morning.services.session_factory() as session:
            urls = [
                e.url
                for e in await session.scalars(
                    select(BriefEntry).where(BriefEntry.brief_id == brief.id)
                )
            ]
        assert urls, day
        assert not seen & set(urls), f"a story came back on {day}"
        seen |= set(urls)
    assert await morning.brief.streak() == 7
    assert len(morning.briefs_on_telegram()) == 7
    assert len(morning.rig.fake.inserted()) == 7
    assert len(morning.push.sent) == 7
    history = await morning.brief.history()
    assert [h["on_time"] for h in history] == [True] * 7


async def test_telegram_brief_command_and_votes(morning: Morning) -> None:
    tuesday = MONDAY + timedelta(days=1)
    morning.clock.set(at(tuesday, 6, 0))
    await morning.bot.handle_update(morning.updates.message("/brief"))
    assert morning.telegram.sent[-1].text == "Today's brief arrives at 07:00."

    morning.news.publish()
    await morning.run_until(at(tuesday, 7, 0))
    first = next(s for s in morning.telegram.sent if s.html)
    assert first.buttons is not None
    row = first.buttons[0]
    vote = parse_vote(row[0]["callback_data"])
    assert vote is not None
    entry_id = vote[0]

    await morning.bot.handle_update(
        morning.updates.press(row[0]["callback_data"], message_id=first.message_id)
    )
    assert morning.telegram.answers[-1][1] == "👍 Noted: more like this."
    async with morning.services.session_factory() as session:
        entry = await session.get(BriefEntry, entry_id)
    assert entry is not None
    assert entry.vote == 1
    assert entry.voted_at == morning.clock.now()
    chat_id, message_id, buttons = morning.telegram.markups[-1]
    assert (chat_id, message_id) == (AMANI["id"], first.message_id)
    assert buttons is not None
    assert buttons[0][0]["text"] == "1 👍 ✓"

    # The same button again takes the vote back; the other one changes it.
    await morning.bot.handle_update(morning.updates.press(row[0]["callback_data"]))
    assert morning.telegram.answers[-1][1] == "Vote taken back."
    await morning.bot.handle_update(morning.updates.press(row[1]["callback_data"]))
    assert morning.telegram.answers[-1][1] == "👎 Noted: less like this."
    async with morning.services.session_factory() as session:
        entry = await session.get(BriefEntry, entry_id)
    assert entry is not None
    assert entry.vote == -1

    # A stranger's taps do nothing.
    stranger = {"id": 5_550_666, "is_bot": False, "first_name": "Eve"}
    await morning.bot.handle_update(morning.updates.press(row[0]["callback_data"], sender=stranger))
    async with morning.services.session_factory() as session:
        entry = await session.get(BriefEntry, entry_id)
    assert entry is not None
    assert entry.vote == -1

    # /brief sends today's again, with its voice note.
    before = len(morning.briefs_on_telegram())
    await morning.bot.handle_update(morning.updates.message("/brief"))
    assert len(morning.briefs_on_telegram()) == before + 1
    assert len(morning.telegram.voice_notes) == 2


async def test_old_briefs_audio_and_stories_are_forgotten(morning: Morning) -> None:
    tuesday = MONDAY + timedelta(days=1)
    morning.clock.set(at(tuesday, 6, 30))
    morning.news.publish()
    await morning.run_until(at(tuesday, 7, 0))
    brief = await morning.brief_on(tuesday)
    assert brief is not None
    assert brief.audio_file is not None
    assert morning.brief.audio_path(brief.audio_file) is not None
    assert morning.brief.audio_path("../secrets.mp3") is None

    morning.clock.set(at(tuesday + timedelta(days=31), 3, 0))
    purged = await morning.brief.purge()
    assert purged["audio"] == 1
    assert purged["briefs"] == 0  # your votes on it still count for 60 days
    kept = await morning.brief_on(tuesday)
    assert kept is not None
    assert kept.audio_file is None
    assert purged["stories"] > 0

    morning.clock.set(at(tuesday + timedelta(days=61), 3, 0))
    assert (await morning.brief.purge())["briefs"] == 1
    assert await morning.brief_on(tuesday) is None


def test_the_script_is_spoken_in_pieces_between_sentences() -> None:
    script = " ".join(f"Sentence number {n} is here." for n in range(100))
    parts = spoken_parts(script, 120)
    assert all(len(p) <= 120 for p in parts)
    assert " ".join(parts) == script
    assert all(p.endswith(".") for p in parts)
    long_word_run = "word " * 100
    assert all(len(p) <= 50 for p in spoken_parts(long_word_run, 50))
    assert spoken_parts("") == []


def test_the_inbox_copys_mark_is_encrypted() -> None:
    vault = Vault(Vault.generate_key())
    brief_id = uuid.uuid4()
    mark = brief_mark(vault, brief_id)
    assert str(brief_id).encode() not in base64.urlsafe_b64decode(mark)
    assert is_brief_mark(vault, mark)
    assert not is_brief_mark(Vault(Vault.generate_key()), mark)  # another Jarvis's key
