"""Your tech brief every morning: read hourly, prepared at 06:40, delivered at 07:00.

A small loop inside Jarvis keeps the time. It wakes every 30 seconds, or right
when the next step is due, and compares the clock with the database:

* **Reading:** every source, hourly, so a site that's down at 07:00 is already
  covered.
* **Preparing:** from `time - prepare_minutes` (06:40): the latest news, ranked,
  summarised and noted for you.
* **Delivering:** at `time` (07:00), to each channel once: the app, a push,
  Telegram (with 👍/👎 and a voice note) and a copy placed in your inbox.
* **Catching up:** if Jarvis was off at 07:00, the brief goes out when it's
  back, the same day. A day that has passed is skipped, never sent late.

Everything it decides is stored, so a restart at any moment carries on where
it stopped, and no channel gets the same brief twice. A channel that fails
(Telegram unreachable, say) is tried again two minutes later, then every half
hour that day.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update

from jarvis.brief.build import TASTE_DAYS, BriefBuilder
from jarvis.brief.config import (
    BriefConfig,
    BriefConfigError,
    SourcesConfig,
    load_brief_config,
    load_sources_config,
)
from jarvis.brief.reader import BriefReader
from jarvis.brief.render import (
    BriefView,
    Buttons,
    build_view,
    email_html,
    email_subject,
    email_text,
    push_text,
    telegram_messages,
    vote_data,
)
from jarvis.config import Settings
from jarvis.db.models import Brief, BriefEntry, BriefSource
from jarvis.db.session import transaction
from jarvis.mail.mime import BRIEF_HEADER, brief_mark
from jarvis.security.fetch import SafeFetcher
from jarvis.services import Services
from jarvis.voice.speech import TTS_SAMPLE_RATE

if TYPE_CHECKING:
    from jarvis.mail.service import MailService

log = logging.getLogger("jarvis.brief")

ON_TIME = timedelta(minutes=5)  # delivered this soon after 07:00 counts as on time
TICK_SECONDS = 30.0
READ_EVERY = timedelta(hours=1)
FIRST_RETRY = timedelta(minutes=2)  # a failed channel: tried again soon, then half-hourly
RETRY_EVERY = timedelta(minutes=30)
SPOKEN_CHARS = 900  # per request to the speech server
PAUSE_SECONDS = 0.4  # between spoken parts
BRIEF_LABEL = "Jarvis/Brief"
SENT, FAILED, SKIPPED = "sent", "failed", "skipped"
_AUDIO_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.mp3$")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


class BriefTelegram(Protocol):
    async def send_brief(
        self, messages: list[tuple[str, Buttons]], *, silent: bool
    ) -> list[int]: ...

    async def send_brief_audio(self, audio: bytes, *, caption: str, silent: bool) -> bool: ...


class Speaker(Protocol):
    async def synthesize(self, text: str, *, response_format: str = "mp3") -> bytes: ...


class BriefNotFound(LookupError):
    pass


def spoken_parts(text: str, limit: int = SPOKEN_CHARS) -> list[str]:
    """The script in pieces the speech server takes comfortably, split between sentences."""
    parts: list[str] = []
    current = ""
    for sentence in (s.strip() for s in _SENTENCE.split(text.strip())):
        while len(sentence) > limit:  # one very long sentence: split it between words
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                parts.append(current)
                current = ""
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def encode_mp3(pcm: bytes, rate: int = TTS_SAMPLE_RATE) -> bytes:
    """16-bit mono PCM as MP3 (libsndfile, bundled with soundfile: no extra tools)."""
    import numpy as np
    import soundfile

    samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
    buffer = io.BytesIO()
    soundfile.write(buffer, samples, rate, format="MP3", subtype="MPEG_LAYER_III")
    return buffer.getvalue()


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


class BriefService:
    def __init__(
        self,
        services: Services,
        *,
        config: BriefConfig,
        sources: SourcesConfig,
        fetcher: SafeFetcher,
        mail: MailService | None = None,
        speech: Callable[[], Speaker | None] | None = None,
        audio_dir: Path | None = None,
    ) -> None:
        self._s = services
        self.config = config
        self.sources = sources
        self.reader = BriefReader(services, config=config, sources=sources, fetcher=fetcher)
        self.builder = BriefBuilder(services, config=config, sources=sources, fetcher=fetcher)
        self._mail = mail
        self._speech = speech or (lambda: None)
        self.audio_dir = audio_dir or services.settings.data_dir / "briefs"
        self.telegram: BriefTelegram | None = None  # attached once the bot is running
        origin = services.settings.public_origin.rstrip("/")
        self.app_link = f"{origin}/brief"
        self._deliver_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._next_read: datetime | None = None
        self._next_retry: datetime | None = None
        self._making: asyncio.Task[None] | None = None
        self._fetcher = fetcher
        self.last_error: str | None = None

    async def aclose(self) -> None:
        if self._making is not None:
            self._making.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._making
        await self._fetcher.aclose()

    # --- Time ------------------------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return self._s.policies_config.tz

    def today(self) -> date:
        return self._s.clock.now().astimezone(self.tz).date()

    def delivery_time(self, day: date) -> datetime:
        return self.builder.scheduled_for(day)

    def prepare_time(self, day: date) -> datetime:
        return self.delivery_time(day) - timedelta(minutes=self.config.prepare_minutes)

    def source_names(self) -> dict[str, str]:
        return {source.id: source.name for source in self.sources.sources}

    @property
    def _telegram_link(self) -> str | None:
        return self.app_link if self.app_link.startswith("https://") else None  # Telegram's rule

    # --- The loop --------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Read every hour, and prepare and deliver on time, until `stop` is set."""
        await asyncio.gather(self._reading(stop), self._timing(stop))

    async def _reading(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.read()
            except Exception:
                log.exception("brief: reading the sources failed")
            await _sleep(stop, READ_EVERY.total_seconds())

    async def _timing(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
                self.last_error = None
            except Exception as exc:
                log.exception("brief: preparing or delivering failed")
                self.last_error = _short(exc)
            await _sleep(stop, self.seconds_to_next())

    def seconds_to_next(self) -> float:
        """Until the next step is due, but never longer than a tick (the clock may jump)."""
        now = self._s.clock.now()
        day = self.today()
        due = [t for t in (self.prepare_time(day), self.delivery_time(day)) if t > now]
        wait = min(((t - now).total_seconds() for t in due), default=TICK_SECONDS)
        return max(1.0, min(TICK_SECONDS, wait))

    async def read(self, *, force: bool = False) -> None:
        async with self._read_lock:
            await self.reader.read_round(force=force)

    async def tick(self, *, read: bool = False) -> Brief | None:
        """Do whatever is due now: prepare, deliver, or try failed channels again."""
        now = self._s.clock.now()
        if read and (self._next_read is None or now >= self._next_read):
            self._next_read = now + READ_EVERY
            await self.read()
        day = self.today()
        brief = await self.brief_for(day)
        if brief is None and now >= self.prepare_time(day):
            await self.read()  # the latest, for a brief made now
            brief = await self.builder.prepare(day)
        if brief is None:
            return None
        if brief.status == "ready" and now >= self.delivery_time(day):
            return await self.deliver(day)
        failed = any(d.get("status") == FAILED for d in (brief.deliveries or {}).values())
        retry_due = self._next_retry is None or now >= self._next_retry  # None: just restarted
        if brief.status == "delivered" and failed and retry_due:
            return await self.deliver(day)
        return brief

    async def make_now(self) -> Brief:
        """ "Make today's brief now": ready straight away, and delivered if 07:00 has passed."""
        day = self.today()
        await self.read()
        brief = await self.builder.prepare(day)
        if brief.status == "ready" and self._s.clock.now() >= self.delivery_time(day):
            brief = await self.deliver(day) or brief
        return brief

    @property
    def making(self) -> bool:
        return self._making is not None and not self._making.done()

    def start_making(self) -> bool:
        """Start "make it now" in the background. False if it's already under way."""
        if self.making:
            return False
        self._making = asyncio.create_task(self._make_in_background())
        return True

    async def _make_in_background(self) -> None:
        try:
            await self.make_now()
        except Exception as exc:
            log.exception("brief: making today's brief failed")
            self.last_error = _short(exc)

    # --- Delivering ------------------------------------------------------------------------

    async def brief_for(self, day: date) -> Brief | None:
        async with self._s.session_factory() as session:
            return await session.scalar(select(Brief).where(Brief.day == day))

    async def _update(self, brief_id: uuid.UUID, **values: Any) -> None:
        values["updated_at"] = self._s.clock.now()
        async with transaction(self._s.session_factory) as session:
            await session.execute(update(Brief).where(Brief.id == brief_id).values(**values))

    def _channels(self) -> list[tuple[str, Callable[..., Awaitable[dict[str, Any]]]]]:
        on = self.config.channels
        channels: list[tuple[str, Callable[..., Awaitable[dict[str, Any]]]]] = []
        if on.app:
            channels.append(("app", self._to_app))
        if on.audio:
            channels.append(("audio", self._to_audio))
        if on.push:
            channels.append(("push", self._to_push))
        if on.telegram:
            channels.append(("telegram", self._to_telegram))
            if on.audio:
                channels.append(("voice", self._to_voice))
        if on.inbox:
            channels.append(("inbox", self._to_inbox))
        return channels

    async def deliver(self, day: date) -> Brief | None:
        """Send the brief for `day` wherever it hasn't gone yet. Only on that day."""
        async with self._deliver_lock:
            brief = await self.brief_for(day)
            if brief is None or brief.status not in ("ready", "delivered"):
                return brief
            now = self._s.clock.now()
            if day != self.today():  # its day has passed: it's news no longer
                if brief.status == "ready":
                    await self._update(brief.id, status="skipped")
                return await self.brief_for(day)
            first = brief.status == "ready"
            if first:
                on_time = now <= brief.scheduled_for + ON_TIME
                await self._update(brief.id, status="delivered", delivered_at=now, on_time=on_time)
                if not on_time:
                    log.info("brief: delivered %s late", now - brief.scheduled_for)
            view = await self.view_of(brief)
            quiet = self._s.policies_config.quiet_at(now)
            done: dict[str, Any] = dict(brief.deliveries or {})
            for channel, send in self._channels():
                if (done.get(channel) or {}).get("status") in (SENT, SKIPPED):
                    continue
                try:
                    result = await send(brief, view, quiet, done)
                except Exception as exc:  # this channel waits for the next try
                    log.warning("brief: couldn't deliver to %s: %s", channel, _short(exc))
                    result = {"status": FAILED, "error": _short(exc)}
                done[channel] = {"at": self._s.clock.now().isoformat(), **result}
                await self._update(brief.id, deliveries=done)
            if any(d.get("status") == FAILED for d in done.values()):
                self._next_retry = now + (FIRST_RETRY if first else RETRY_EVERY)
            return await self.brief_for(day)

    async def _to_app(
        self, brief: Brief, view: BriefView, quiet: bool, done: dict[str, Any]
    ) -> dict[str, Any]:
        return {"status": SENT}

    async def _to_audio(
        self, brief: Brief, view: BriefView, quiet: bool, done: dict[str, Any]
    ) -> dict[str, Any]:
        speaker = self._speech()
        if speaker is None:
            return {"status": SKIPPED, "detail": "Voice is off, so there's no audio version."}
        if not view.script.strip():
            return {"status": SKIPPED, "detail": "Nothing to read aloud."}
        pcm = bytearray()
        pause = b"\x00\x00" * int(TTS_SAMPLE_RATE * PAUSE_SECONDS)
        for n, part in enumerate(spoken_parts(view.script)):
            spoken = await speaker.synthesize(part, response_format="pcm")
            pcm += (pause if n else b"") + spoken
        if len(pcm) < TTS_SAMPLE_RATE:  # under half a second: something went wrong
            raise RuntimeError("the speech server sent back no audio")
        data = await asyncio.to_thread(encode_mp3, bytes(pcm))
        name = f"{brief.day.isoformat()}.mp3"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        partial = self.audio_dir / f".{name}.part"
        partial.write_bytes(data)
        partial.replace(self.audio_dir / name)
        seconds = round(len(pcm) / 2 / TTS_SAMPLE_RATE)
        await self._update(brief.id, audio_file=name, audio_seconds=seconds)
        return {"status": SENT, "seconds": seconds}

    async def _to_push(
        self, brief: Brief, view: BriefView, quiet: bool, done: dict[str, Any]
    ) -> dict[str, Any]:
        if quiet:  # phones always sound for a push
            return {"status": SKIPPED, "detail": "Quiet hours"}
        if not self._s.push.configured:
            return {"status": SKIPPED, "detail": "Notifications aren't set up"}
        title, body = push_text(view)
        result = await self._s.push.send(title=title, body=body, url="/brief")
        if result.delivered == 0:
            return {"status": SKIPPED, "detail": "No device has notifications on"}
        return {"status": SENT, "devices": result.delivered}

    async def _to_telegram(
        self, brief: Brief, view: BriefView, quiet: bool, done: dict[str, Any]
    ) -> dict[str, Any]:
        if self.telegram is None:
            return {"status": SKIPPED, "detail": "Telegram isn't set up"}
        messages = telegram_messages(view, app_link=self._telegram_link)
        ids = await self.telegram.send_brief(messages, silent=quiet)
        if not ids:
            return {"status": SKIPPED, "detail": "Telegram isn't linked"}
        return {"status": SENT, "messages": ids}

    async def _to_voice(
        self, brief: Brief, view: BriefView, quiet: bool, done: dict[str, Any]
    ) -> dict[str, Any]:
        audio, telegram = done.get("audio") or {}, done.get("telegram") or {}
        if SKIPPED in (telegram.get("status"), audio.get("status")):
            return {"status": SKIPPED, "detail": "No Telegram, or no audio version"}
        if telegram.get("status") != SENT or audio.get("status") != SENT:
            return {"status": FAILED, "error": "Waiting for the brief and its audio to go out"}
        fresh = await self.brief_for(brief.day)
        if self.telegram is None or fresh is None or not fresh.audio_file:
            return {"status": FAILED, "error": "The audio version isn't ready yet"}
        audio_bytes = await asyncio.to_thread((self.audio_dir / fresh.audio_file).read_bytes)
        minutes = max(1, round((fresh.audio_seconds or 0) / 60))
        caption = f"🎧 Your tech brief, read aloud ({minutes} min)"
        await self.telegram.send_brief_audio(audio_bytes, caption=caption, silent=True)
        return {"status": SENT}

    async def _to_inbox(
        self, brief: Brief, view: BriefView, quiet: bool, done: dict[str, Any]
    ) -> dict[str, Any]:
        if self._mail is None:
            return {"status": SKIPPED, "detail": "Gmail isn't connected"}
        access = await self._mail.access()
        if access.level == "none" or not access.account:
            return {"status": SKIPPED, "detail": "Gmail isn't connected"}
        if access.level != "full":
            return {
                "status": SKIPPED,
                "detail": "Jarvis can only read your Gmail. For a copy in your inbox, "
                "give Jarvis your inbox in Sources.",
            }
        raw, message_id = self.inbox_copy(brief, view, account=access.account)
        client = self._mail.client()
        # A copy placed before (its answer lost, say) is found, not placed twice.
        found = await client.find_message(f"rfc822msgid:{message_id.strip('<>')}")
        if found is not None:
            return {"status": SENT, "id": found}
        label = await self._mail.ensure_label(BRIEF_LABEL)
        placed = await client.insert_message(raw, ["INBOX", "UNREAD", label])
        return {"status": SENT, "id": str(placed.get("id") or "")}

    def inbox_copy(self, brief: Brief, view: BriefView, *, account: str) -> tuple[str, str]:
        """The brief as an email from Jarvis to you, and its Message-ID."""
        message_id = f"<brief-{brief.id}@jarvis.local>"
        link = self.app_link if self.app_link.startswith(("https://", "http://")) else None
        # Headers stay on one line: the mark must reach Gmail exactly as Jarvis made it.
        msg = EmailMessage(policy=policy.SMTP.clone(max_line_length=998))
        msg["From"] = formataddr(("Jarvis", account))
        msg["To"] = account
        msg["Subject"] = email_subject(view)
        msg["Date"] = format_datetime(self._s.clock.now().astimezone(self.tz))
        msg["Message-ID"] = message_id
        msg["Auto-Submitted"] = "auto-generated"
        msg[BRIEF_HEADER] = brief_mark(self._s.vault, brief.id)
        msg.set_content(email_text(view, app_link=link))
        msg.add_alternative(email_html(view, app_link=link), subtype="html")
        return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii"), message_id

    # --- Telegram ------------------------------------------------------------------------------

    async def telegram_again(self) -> str | None:
        """/brief: today's brief on Telegram again. A note instead when there's none yet."""
        day = self.today()
        brief = await self.brief_for(day)
        if brief is None or brief.status not in ("ready", "delivered"):
            at = self.delivery_time(day)
            if self._s.clock.now() < at:
                return f"Today's brief arrives at {at.astimezone(self.tz):%H:%M}."
            return "There's no brief today yet. In the app, open Brief and tap “Make it now”."
        if self.telegram is None:
            return "Telegram isn't set up for the brief."
        view = await self.view_of(brief)
        await self.telegram.send_brief(
            telegram_messages(view, app_link=self._telegram_link), silent=False
        )
        if brief.audio_file and (self.audio_dir / brief.audio_file).is_file():
            audio = await asyncio.to_thread((self.audio_dir / brief.audio_file).read_bytes)
            await self.telegram.send_brief_audio(
                audio, caption="🎧 Your tech brief, read aloud", silent=True
            )
        return None

    async def telegram_vote(self, entry_id: uuid.UUID, vote: int) -> tuple[str, Buttons | None]:
        """A 👍/👎 tap: the same button again takes the vote back."""
        entry = await self.vote(entry_id, vote, toggle=True)
        if entry is None:
            return "That story isn't in Jarvis any more.", None
        answer = {1: "👍 Noted: more like this.", -1: "👎 Noted: less like this."}.get(
            entry.vote or 0, "Vote taken back."
        )
        view = await self.view(brief_id=entry.brief_id)
        for _, buttons in telegram_messages(view, app_link=self._telegram_link):
            if any(row[0]["callback_data"] == vote_data(entry_id, 1) for row in buttons):
                return answer, buttons
        return answer, None

    # --- Feedback ----------------------------------------------------------------------------

    async def vote(
        self, entry_id: uuid.UUID, vote: int | None, *, toggle: bool = False
    ) -> BriefEntry | None:
        """Your 👍 (1), 👎 (-1) or neither (None) on a story. `toggle`: the same vote clears it."""
        if vote not in (1, -1, None):
            raise ValueError("a vote is 1, -1 or None")
        async with transaction(self._s.session_factory) as session:
            entry = await session.get(BriefEntry, entry_id, with_for_update=True)
            if entry is None:
                return None
            if toggle and entry.vote == vote:
                vote = None
            entry.vote = vote
            entry.voted_at = self._s.clock.now() if vote is not None else None
            return entry

    # --- Reading it ----------------------------------------------------------------------------

    async def view_of(self, brief: Brief) -> BriefView:
        async with self._s.session_factory() as session:
            entries = list(
                await session.scalars(select(BriefEntry).where(BriefEntry.brief_id == brief.id))
            )
        return build_view(
            brief, entries, decrypt=self.builder.dec, source_names=self.source_names()
        )

    async def view(
        self, *, day: date | None = None, brief_id: uuid.UUID | None = None
    ) -> BriefView:
        async with self._s.session_factory() as session:
            if brief_id is not None:
                brief = await session.get(Brief, brief_id)
            else:
                brief = await session.scalar(
                    select(Brief).where(Brief.day == (day or self.today()))
                )
        if brief is None:
            raise BriefNotFound("No brief for that day.")
        return await self.view_of(brief)

    async def history(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Past briefs, newest first: the day, how it went and its top headline."""
        async with self._s.session_factory() as session:
            briefs = list(
                await session.scalars(select(Brief).order_by(Brief.day.desc()).limit(limit))
            )
            tops = {
                entry.brief_id: entry.title
                for entry in await session.scalars(
                    select(BriefEntry).where(
                        BriefEntry.brief_id.in_([b.id for b in briefs]),
                        BriefEntry.section == "top",
                        BriefEntry.rank == 1,
                    )
                )
            }
        return [
            {
                "day": b.day.isoformat(),
                "status": b.status,
                "on_time": b.on_time,
                "delivered_at": b.delivered_at.isoformat() if b.delivered_at else None,
                "headline": tops.get(b.id),
            }
            for b in briefs
        ]

    def next_delivery(self, today_status: str | None) -> datetime:
        """When the next brief arrives: today's, unless it's been and gone."""
        day = self.today()
        if today_status in ("delivered", "skipped") or (
            today_status is None and self._s.clock.now() >= self.delivery_time(day)
        ):
            day += timedelta(days=1)
        return self.delivery_time(day)

    async def source_health(self) -> list[dict[str, Any]]:
        """Each source in sources.yaml and how reading it went."""
        async with self._s.session_factory() as session:
            states = {row.id: row for row in await session.scalars(select(BriefSource))}
        out: list[dict[str, Any]] = []
        for source in self.sources.sources:
            state = states.get(source.id)
            out.append(
                {
                    "id": source.id,
                    "name": source.name,
                    "category": source.category,
                    "enabled": source.enabled,
                    "last_ok_at": state.last_ok_at.isoformat()
                    if state and state.last_ok_at
                    else None,
                    "last_checked_at": state.last_checked_at.isoformat()
                    if state and state.last_checked_at
                    else None,
                    "failures": state.failures if state else 0,
                    "last_error": state.last_error if state else None,
                }
            )
        return out

    async def streak(self) -> int:
        """How many days in a row the brief has arrived on time, up to today."""
        today = self.today()
        async with self._s.session_factory() as session:
            rows = (
                await session.execute(
                    select(Brief.day, Brief.on_time)
                    .where(Brief.day <= today)
                    .order_by(Brief.day.desc())
                )
            ).all()
        on_time: dict[date, bool | None] = {row.day: row.on_time for row in rows}
        day = today if on_time.get(today) is not None else today - timedelta(days=1)
        count = 0
        while on_time.get(day) is True:
            count += 1
            day -= timedelta(days=1)
        return count

    # --- Forgetting ------------------------------------------------------------------------------

    async def purge(self) -> dict[str, int]:
        """Nightly: old stories, audio files and briefs. Your votes count for 60 days."""
        items = await self.reader.purge()
        today = self.today()
        audio_cutoff = today - timedelta(days=self.config.keep_days)
        brief_cutoff = today - timedelta(days=max(self.config.keep_days, TASTE_DAYS))
        removed = 0
        if self.audio_dir.is_dir():
            for path in self.audio_dir.iterdir():
                if not _AUDIO_NAME.match(path.name):
                    continue
                with contextlib.suppress(ValueError):
                    if date.fromisoformat(path.stem) < audio_cutoff:
                        path.unlink(missing_ok=True)
                        removed += 1
        async with transaction(self._s.session_factory) as session:
            await session.execute(
                update(Brief)
                .where(Brief.day < audio_cutoff, Brief.audio_file.is_not(None))
                .values(audio_file=None)
            )
            result = await session.execute(delete(Brief).where(Brief.day < brief_cutoff))
        briefs = int(getattr(result, "rowcount", 0) or 0)
        return {"stories": items, "audio": removed, "briefs": briefs}

    def audio_path(self, name: str) -> Path | None:
        """A brief's audio file, if `name` is one (and nothing else on disk)."""
        if not _AUDIO_NAME.match(name):
            return None
        path = self.audio_dir / name
        return path if path.is_file() else None


def build_brief_service(
    settings: Settings,
    services: Services,
    *,
    mail: MailService | None,
    speech: Callable[[], Speaker | None] | None,
    fetcher: SafeFetcher | None = None,
) -> tuple[BriefService | None, str | None]:
    """The brief, if its settings are valid. If not, None and why: the rest of Jarvis runs."""
    try:
        config = load_brief_config(settings.brief_config_path)
        sources = load_sources_config(settings.sources_config_path)
    except BriefConfigError as exc:
        log.error("the tech brief is off: %s", exc)
        return None, str(exc)
    service = BriefService(
        services,
        config=config,
        sources=sources,
        fetcher=fetcher or SafeFetcher(allow_private=settings.fetch_allow_private),
        mail=mail,
        speech=speech,
    )
    return service, None


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
