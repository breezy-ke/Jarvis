"""Alerts for urgent mail from people you know, and the morning and evening digests.

* **Urgent alerts** go out at once by push and Telegram, for mail from people you
  know (`alerts.urgent` in email.yaml). Never in quiet hours, and at most
  `max_per_hour`: anything held back is in the next digest.
* **Digests** at the times in email.yaml, built by code from what triage found:
  counts, what needs a reply, and dates coming up. No model writes them.
* **Back online:** after a gap in syncing (a power cut, say), one note says how
  many emails Jarvis caught up on.

The latest digest is kept encrypted, for the Home page and the Inbox.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select

from jarvis.db.models import MailMessage, MailThread, SystemState
from jarvis.db.session import transaction
from jarvis.mail.config import EmailConfig
from jarvis.mail.inbox import Inbox
from jarvis.mail.store import MailStore
from jarvis.mail.sync import MailAccess, SyncReport
from jarvis.mail.triage import Triage, details
from jarvis.notify.owner import OwnerNotifier
from jarvis.services import Services

log = logging.getLogger("jarvis.mail")

DIGEST_KEY = "mail_digest"
ALERT_WINDOW = timedelta(hours=1)
STALE_DIGEST = timedelta(hours=3)  # a digest this late (Jarvis was off) is skipped
OUTAGE = timedelta(minutes=15)
UPCOMING = timedelta(hours=48)
DATES_FROM = timedelta(days=30)  # dates are looked for in mail this recent
LIST_LIMIT = 5


def digest_kind(moment: time) -> str:
    return "morning" if moment.hour < 12 else "evening"


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _duration(gap: timedelta) -> str:
    minutes = int(gap.total_seconds() // 60)
    if minutes < 120:
        return _plural(minutes, "minute")
    hours = minutes // 60
    return _plural(hours, "hour") if hours < 48 else _plural(hours // 24, "day")


@dataclass
class DigestItem:
    thread_id: str
    sender: str
    subject: str
    summary: str


@dataclass
class DigestDate:
    what: str
    start: str  # ISO date (all day) or date-time
    all_day: bool
    thread_id: str
    sender: str


@dataclass
class Digest:
    kind: str  # "morning" or "evening"
    created_at: str
    since: str
    new_emails: int
    counts: dict[str, int]
    needs_reply: list[DigestItem]
    upcoming: list[DigestDate]

    @property
    def quiet(self) -> bool:
        """Nothing new, nothing waiting, nothing coming up: no need to disturb you."""
        return not (self.new_emails or self.counts.get("attention") or self.upcoming)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> Digest:
        return cls(
            kind=value["kind"],
            created_at=value["created_at"],
            since=value["since"],
            new_emails=int(value["new_emails"]),
            counts=dict(value["counts"]),
            needs_reply=[DigestItem(**item) for item in value["needs_reply"]],
            upcoming=[DigestDate(**item) for item in value["upcoming"]],
        )


def render(digest: Digest, tz: Any) -> tuple[str, str, str]:
    """(title, a push-sized line, the full text) for a digest."""
    title = "☀️ Morning inbox" if digest.kind == "morning" else "🌙 Evening inbox"
    counts = digest.counts
    bits = [f"{digest.new_emails} new"]
    for key, word, plural in (
        ("attention", "needs attention", "need attention"),
        ("leads", "lead", None),
        ("invoices", "invoice", None),
        ("suspicious", "suspicious", "suspicious"),
    ):
        if counts.get(key):
            bits.append(_plural(counts[key], word, plural))
    short = " · ".join(bits)
    since = datetime.fromisoformat(digest.since).astimezone(tz)
    lines = [f"{_plural(digest.new_emails, 'new email')} since {since:%a %H:%M}. {short}."]
    if digest.needs_reply:
        lines += ["", "Needs your attention:"]
        for item in digest.needs_reply:
            summary = f" ({item.summary})" if item.summary else ""
            lines.append(f"• {item.sender}: {item.subject}{summary}")
    if digest.upcoming:
        lines += ["", "Coming up:"]
        for when in digest.upcoming:
            if when.all_day:
                moment = f"{date.fromisoformat(when.start[:10]):%a %d %b}"
            else:
                moment = f"{datetime.fromisoformat(when.start).astimezone(tz):%a %d %b %H:%M}"
            lines.append(f"• {moment}: {when.what} ({when.sender})")
    if digest.quiet:
        lines = ["All quiet: nothing new and nothing waiting on you."]
    return title, short, "\n".join(lines)


class MailAlerts:
    def __init__(
        self,
        services: Services,
        *,
        store: MailStore,
        inbox: Inbox,
        notifier: OwnerNotifier,
        config: EmailConfig,
        access: Callable[[], Awaitable[MailAccess]],
    ) -> None:
        self._s = services
        self._store = store
        self._inbox = inbox
        self._notifier = notifier
        self._config = config
        self._access = access

    # --- Urgent mail ------------------------------------------------------------------

    def wants_alert(self, triage: Triage) -> bool:
        rule = self._config.alerts.urgent
        if rule == "off" or triage.category == "suspicious":
            return False
        if not (triage.category == "urgent" or (triage.sender.vip and triage.needs_reply)):
            return False
        return rule == "all" or triage.sender.known or triage.sender.vip

    async def after_triage(self, triage: Triage) -> bool:
        """Tell you at once about urgent mail from someone you know. True if it went out."""
        if not self.wants_alert(triage) or self._notifier.quiet_now():
            return False  # it'll be in the next digest
        now = self._s.clock.now()
        store = self._store
        async with transaction(self._s.session_factory) as session:
            recent = await session.scalar(
                select(func.count())
                .select_from(MailThread)
                .where(MailThread.alerted_at > now - ALERT_WINDOW)
            )
            if (recent or 0) >= self._config.alerts.max_per_hour:
                log.info("mail: urgent alert held back (hourly limit); it's in the next digest")
                return False
            thread = await session.get(MailThread, triage.thread_id, with_for_update=True)
            if thread is None:
                return False
            thread.alerted_at = now
            subject = store.dec(thread.subject_enc) or "(no subject)"
            message = await session.get(MailMessage, triage.message_id)
            names = store.dec_json(message.names_enc, {}) if message is not None else {}
        sender = str(names.get(triage.sender_address) or triage.sender_address)
        label = "Urgent" if triage.category == "urgent" else "From a VIP"
        summary = triage.summary or "Open Jarvis to read it."
        delivered = await self._notifier.tell(
            title=f"📧 {label}: {sender}"[:120],
            body=f"{subject}: {summary}"[:240],
            detail=f"{subject}\n{summary}\n\nReply from the Inbox, or ask me to draft one.",
            url=f"/inbox?thread={triage.thread_id}",
        )
        return delivered.anywhere

    # --- After an outage -----------------------------------------------------------------

    async def after_sync(self, report: SyncReport) -> bool:
        """One note after a gap in syncing: how much mail Jarvis caught up on."""
        threshold = max(OUTAGE, timedelta(seconds=3 * self._config.sync.interval_seconds))
        caught_up = len(report.new_inbound)
        if report.gap is None or report.gap < threshold or not caught_up:
            return False
        text = (
            f"Back after {_duration(report.gap)} away: caught up on "
            f"{_plural(caught_up, 'email')}. Anything urgent comes next."
        )
        delivered = await self._notifier.tell(
            title="Jarvis is back online",
            body=text,
            url="/inbox",
            silent=self._notifier.quiet_now(),
        )
        return delivered.anywhere

    async def access_lost(self) -> bool:
        """Google stopped accepting Jarvis's access (changing your password does this)."""
        delivered = await self._notifier.tell(
            title="Reconnect Google",
            body=(
                "Google stopped accepting Jarvis's access to your Gmail, so email is paused. "
                "Connect Google again in Sources: Jarvis carries on from where it stopped."
            ),
            url="/sources",
            silent=self._notifier.quiet_now(),
        )
        return delivered.anywhere

    # --- Digests ---------------------------------------------------------------------------

    async def latest(self) -> Digest | None:
        async with self._s.session_factory() as session:
            row = await session.get(SystemState, DIGEST_KEY)
        if row is None or not row.value.get("enc"):
            return None
        data = self._store.dec_json(str(row.value["enc"]).encode(), None)
        return Digest.from_json(data) if data else None

    async def build(self, kind: str) -> Digest:
        now = self._s.clock.now()
        previous = await self.latest()
        since = (
            datetime.fromisoformat(previous.created_at) if previous else now - timedelta(hours=12)
        )
        async with self._s.session_factory() as session:
            new_emails = await session.scalar(
                select(func.count())
                .select_from(MailMessage)
                .where(MailMessage.direction == "in")
                .where(MailMessage.deleted.is_(False))
                .where(MailMessage.internal_date > since)
            )
        counts = await self._inbox.counts()
        attention = await self._inbox.threads("attention", limit=LIST_LIMIT)
        return Digest(
            kind=kind,
            created_at=now.isoformat(),
            since=since.isoformat(),
            new_emails=int(new_emails or 0),
            counts=counts,
            needs_reply=[
                DigestItem(item.id, item.sender, item.subject, item.summary) for item in attention
            ],
            upcoming=await self._upcoming(now),
        )

    async def _upcoming(self, now: datetime) -> list[DigestDate]:
        tz = self._s.policies_config.tz
        today = now.astimezone(tz).date()
        found: list[tuple[datetime, DigestDate]] = []
        async with self._s.session_factory() as session:
            rows = (
                await session.execute(
                    select(MailThread, MailMessage)
                    .outerjoin(MailMessage, MailMessage.id == MailThread.last_inbound_id)
                    .where(MailThread.in_inbox.is_(True))
                    .where(MailThread.details_enc.is_not(None))
                    .where(MailThread.category.not_in(("suspicious", "newsletter")))
                    .where(MailThread.last_message_at > now - DATES_FROM)
                )
            ).tuples()
            for thread, message in rows:
                names = self._store.dec_json(message.names_enc, {}) if message else {}
                address = message.from_address if message else ""
                sender = str(names.get(address) or address or "you")
                for item in details(self._store, thread).get("dates", []):
                    moment = self._when(item, tz, today, now)
                    if moment is not None:
                        found.append(
                            (
                                moment,
                                DigestDate(
                                    what=str(item.get("what") or "Event")[:120],
                                    start=str(item["start"]),
                                    all_day=bool(item.get("all_day")),
                                    thread_id=thread.id,
                                    sender=sender,
                                ),
                            )
                        )
        found.sort(key=lambda pair: pair[0])
        return [entry for _, entry in found[:LIST_LIMIT]]

    @staticmethod
    def _when(item: dict[str, Any], tz: Any, today: date, now: datetime) -> datetime | None:
        """When a date from an email happens, if that's within the next two days."""
        try:
            if item.get("all_day"):
                day = date.fromisoformat(str(item["start"])[:10])
                if not today <= day <= today + timedelta(days=2):
                    return None
                return datetime.combine(day, time.min, tzinfo=tz)
            moment = datetime.fromisoformat(str(item["start"]))
        except (KeyError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz)
        return moment if now <= moment <= now + UPCOMING else None

    async def save(self, digest: Digest) -> None:
        value = {
            "kind": digest.kind,
            "created_at": digest.created_at,
            "enc": self._store.enc_json(digest.to_json()).decode(),
        }
        now = self._s.clock.now()
        async with transaction(self._s.session_factory) as session:
            row = await session.get(SystemState, DIGEST_KEY, with_for_update=True)
            if row is None:
                session.add(SystemState(key=DIGEST_KEY, value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now

    async def send_digest(
        self, kind: str, *, scheduled_for: datetime | None = None
    ) -> Digest | None:
        """Build, keep and send one digest. None if it's stale or there's no mail."""
        now = self._s.clock.now()
        if scheduled_for is not None and now - scheduled_for > STALE_DIGEST:
            log.info("mail: skipped a %s digest that is %s late", kind, now - scheduled_for)
            return None
        if (await self._access()).level == "none":
            return None
        digest = await self.build(kind)
        await self.save(digest)
        if not digest.quiet:
            title, short, full = render(digest, self._s.policies_config.tz)
            await self._notifier.tell(
                title=title,
                body=short,
                detail=full,
                url="/inbox",
                silent=self._notifier.quiet_now(),
            )
        return digest
