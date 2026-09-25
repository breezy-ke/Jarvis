"""Keep Jarvis's copy of your mail in step with Gmail.

* **First sync:** note Gmail's current history id, then read the last
  `first_sync_days` of mail (inbox, sent and archived, not spam or trash).
* **Then, every minute:** ask Gmail what changed since that history id
  (`history.list`): new and deleted messages, and label changes, so reading or
  archiving something in Gmail shows up in Jarvis too.
* **An expired sync point** (Gmail answers 404, usually after a week or more
  offline) means a fresh full sync. Nothing is lost: Gmail is the source.

The database holds all the state, so a restart just carries on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete

from jarvis.clock import Clock
from jarvis.db.models import MailContact, MailThread, SystemState
from jarvis.db.session import SessionFactory, transaction
from jarvis.mail.config import EmailConfig
from jarvis.mail.gmail import GmailAuthError, GmailClient, GmailError, HistoryExpired
from jarvis.mail.mime import parse_gmail_message
from jarvis.mail.store import CHAT, DRAFT, INBOX, MailStore, Stored

log = logging.getLogger("jarvis.mail")

STATE_KEY = "mail_sync"
MAX_FULL_SYNC = 2_000
FETCH_CONCURRENCY = 4
BATCH = 25


@dataclass
class SyncState:
    history_id: int | None = None
    account: str | None = None
    status: str = "off"  # off, ok, error, reconnect
    error: str | None = None
    access: str = "none"
    last_sync_at: str | None = None
    last_full_sync_at: str | None = None

    @classmethod
    def load(cls, value: dict[str, Any] | None) -> SyncState:
        value = value or {}
        return cls(**{k: value[k] for k in cls.__dataclass_fields__ if k in value})

    def dump(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class SyncReport:
    stored: list[Stored] = field(default_factory=list)
    threads: set[str] = field(default_factory=set)
    full: bool = False
    resynced: bool = False
    gap: timedelta | None = None  # time since the last good sync, if any

    @property
    def new_inbound(self) -> list[Stored]:
        return [s for s in self.stored if s.new and s.direction == "in"]

    @property
    def new_outbound(self) -> list[Stored]:
        return [s for s in self.stored if s.new and s.direction == "out"]


@dataclass(frozen=True)
class MailAccess:
    """What the Google connection allows right now."""

    level: str  # "full", "read" or "none"
    account: str | None
    can_add_holds: bool = False


ClientFactory = Callable[[], GmailClient]
AccessCheck = Callable[[], Awaitable[MailAccess]]


class MailSync:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        clock: Clock,
        store: MailStore,
        config: EmailConfig,
        client: ClientFactory,
        access: AccessCheck,
    ) -> None:
        self._sf = session_factory
        self._clock = clock
        self._store = store
        self._config = config
        self._client = client
        self._access = access
        self._lock = asyncio.Lock()

    # --- State -----------------------------------------------------------------------

    async def state(self) -> SyncState:
        async with self._sf() as session:
            row = await session.get(SystemState, STATE_KEY)
            return SyncState.load(row.value if row else None)

    async def _save(self, state: SyncState) -> None:
        async with transaction(self._sf) as session:
            row = await session.get(SystemState, STATE_KEY, with_for_update=True)
            if row is None:
                session.add(
                    SystemState(key=STATE_KEY, value=state.dump(), updated_at=self._clock.now())
                )
            else:
                row.value = state.dump()
                row.updated_at = self._clock.now()

    # --- One round ---------------------------------------------------------------------

    async def sync_once(self) -> SyncReport | None:
        """Bring Jarvis up to date. None when there's nothing to sync from."""
        async with self._lock:
            return await self._sync_once()

    async def _sync_once(self) -> SyncReport | None:
        state = await self.state()
        access = await self._access()
        state.access = access.level
        if access.level == "none":
            state.status, state.error = "off", None
            await self._save(state)
            return None
        if state.account and access.account and state.account != access.account:
            # A different Gmail account: the stored mail isn't its mail.
            log.warning("mail: the connected Google account changed; starting afresh")
            await self._forget_all()
            state = SyncState(access=access.level)
        state.account = access.account or state.account
        client = self._client()
        now = self._clock.now()
        gap = now - datetime.fromisoformat(state.last_sync_at) if state.last_sync_at else None
        try:
            if state.history_id is None:
                report = await self._full(client, state)
            else:
                try:
                    report = await self._incremental(client, state)
                except HistoryExpired:
                    log.info("mail: the sync point expired; doing a full sync")
                    report = await self._full(client, state)
                    report.resynced = True
        except GmailAuthError as exc:
            state.status, state.error = "reconnect", str(exc)[:500]
            await self._save(state)
            raise
        except (GmailError, httpx.HTTPError) as exc:
            state.status = "error"
            state.error = f"{type(exc).__name__}: {exc}"[:500]
            await self._save(state)
            raise
        report.gap = gap
        state.status, state.error = "ok", None
        state.last_sync_at = now.isoformat()
        if report.full:
            state.last_full_sync_at = now.isoformat()
        await self._save(state)
        return report

    async def _forget_all(self) -> None:
        async with transaction(self._sf) as session:
            await session.execute(delete(MailThread))  # messages and drafts go with them
            await session.execute(delete(MailContact))

    async def _full(self, client: GmailClient, state: SyncState) -> SyncReport:
        profile = await client.profile()
        history_id = int(profile["historyId"])  # before listing, so nothing slips between
        days = self._config.sync.first_sync_days
        ids: list[str] = []
        page: str | None = None
        while len(ids) < MAX_FULL_SYNC:
            data = await client.list_messages(
                query=f"newer_than:{days}d -in:chats -in:drafts", page_token=page
            )
            ids.extend(str(m["id"]) for m in data.get("messages") or [])
            page = data.get("nextPageToken")
            if not page:
                break
        report = SyncReport(full=True)
        await self._fetch_and_store(client, ids[:MAX_FULL_SYNC], state, report)
        state.history_id = history_id
        return report

    async def _incremental(self, client: GmailClient, state: SyncState) -> SyncReport:
        assert state.history_id is not None
        added: dict[str, None] = {}
        deleted: set[str] = set()
        labels: dict[str, list[str]] = {}
        latest = state.history_id
        page: str | None = None
        while True:
            data = await client.history(state.history_id, page)
            for record in data.get("history") or []:
                for item in record.get("messagesAdded") or []:
                    added[str(item["message"]["id"])] = None
                for item in record.get("messagesDeleted") or []:
                    deleted.add(str(item["message"]["id"]))
                for key in ("labelsAdded", "labelsRemoved"):
                    for item in record.get(key) or []:
                        message = item["message"]
                        labels[str(message["id"])] = [str(x) for x in message.get("labelIds") or []]
            latest = max(latest, int(data.get("historyId") or latest))
            page = data.get("nextPageToken")
            if not page:
                break
        report = SyncReport()
        new_ids = [m for m in added if m not in deleted]
        await self._fetch_and_store(client, new_ids, state, report)
        async with transaction(self._sf) as session:
            unknown_in_inbox: list[str] = []
            for message_id, label_ids in labels.items():
                if message_id in deleted or message_id in added:
                    continue
                thread_id = await self._store.set_labels(session, message_id, label_ids)
                if thread_id is not None:
                    report.threads.add(thread_id)
                elif INBOX in label_ids:
                    unknown_in_inbox.append(message_id)  # an older message moved to the inbox
            for message_id in deleted:
                thread_id = await self._store.mark_deleted(session, message_id)
                if thread_id is not None:
                    report.threads.add(thread_id)
            for thread_id in report.threads:
                await self._store.refresh_thread(session, thread_id)
        if unknown_in_inbox:
            await self._fetch_and_store(client, unknown_in_inbox, state, report)
        state.history_id = latest
        return report

    async def _fetch_and_store(
        self, client: GmailClient, ids: list[str], state: SyncState, report: SyncReport
    ) -> None:
        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def fetch(message_id: str) -> dict[str, Any] | None:
            async with semaphore:
                return await client.get_message(message_id)

        owner = (state.account or "").lower()
        for start in range(0, len(ids), BATCH):
            resources = await asyncio.gather(*(fetch(m) for m in ids[start : start + BATCH]))
            async with transaction(self._sf) as session:
                touched: set[str] = set()
                for resource in resources:
                    if resource is None:
                        continue
                    labels = set(resource.get("labelIds") or [])
                    if DRAFT in labels or CHAT in labels:
                        continue  # drafts (yours and Jarvis's) aren't mail yet
                    stored = await self._store.upsert(
                        session, parse_gmail_message(resource), owner=owner
                    )
                    report.stored.append(stored)
                    touched.add(stored.thread_id)
                for thread_id in touched:
                    await self._store.refresh_thread(session, thread_id)
                report.threads |= touched
