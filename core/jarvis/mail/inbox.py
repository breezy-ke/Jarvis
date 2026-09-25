"""Reading the inbox as Jarvis sorted it: tabs, counts, and finding a conversation.

A thread's category is yours when you've corrected it ("Is this right?"),
otherwise the one triage gave it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, select

from jarvis.db.models import MailMessage, MailThread
from jarvis.mail.store import MailStore
from jarvis.services import Services

TABS: tuple[str, ...] = (
    "attention",
    "leads",
    "invoices",
    "fyi",
    "newsletters",
    "suspicious",
    "all",
)
SEARCH_WINDOW = 300  # the most recent conversations searched by sender or subject

category = func.coalesce(MailThread.owner_category, MailThread.category)


def tab_filter(tab: str) -> ColumnElement[bool]:
    inbox = MailThread.in_inbox.is_(True)
    by_category = {
        "leads": "lead",
        "invoices": "invoice",
        "fyi": "fyi",
        "newsletters": "newsletter",
        "suspicious": "suspicious",
    }
    if tab == "attention":
        return and_(
            inbox,
            category.not_in(("suspicious", "newsletter")),
            (MailThread.needs_reply.is_(True)) | (category == "urgent"),
        )
    if tab in by_category:
        return and_(inbox, category == by_category[tab])
    if tab == "all":
        return inbox
    raise ValueError(f"unknown inbox tab {tab!r}")


@dataclass(frozen=True)
class ThreadItem:
    id: str
    subject: str
    sender: str  # their name when they gave one, else the address
    sender_address: str
    category: str | None
    priority: int | None
    needs_reply: bool
    unread: bool
    in_inbox: bool
    summary: str
    signals: list[str]
    last_message_at: datetime


class Inbox:
    def __init__(self, services: Services, store: MailStore) -> None:
        self._s = services
        self._store = store

    async def counts(self) -> dict[str, int]:
        async with self._s.session_factory() as session:
            row = (
                await session.execute(
                    select(*(func.count().filter(tab_filter(tab)) for tab in TABS))
                )
            ).one()
        return dict(zip(TABS, (int(n) for n in row), strict=True))

    async def threads(
        self, tab: str, *, limit: int = 30, before: datetime | None = None
    ) -> list[ThreadItem]:
        query = self._select().where(tab_filter(tab))
        if before is not None:
            query = query.where(MailThread.last_message_at < before)
        if tab == "attention":
            query = query.order_by(
                MailThread.priority.desc().nulls_last(), MailThread.last_message_at.desc()
            )
        else:
            query = query.order_by(MailThread.last_message_at.desc())
        async with self._s.session_factory() as session:
            rows = (await session.execute(query.limit(limit))).tuples().all()
        return [self._item(thread, message) for thread, message in rows]

    async def find(self, words: str, *, limit: int = 8) -> list[ThreadItem]:
        """Recent conversations whose sender or subject has all of `words`."""
        wanted = words.casefold().split()
        query = self._select().order_by(MailThread.last_message_at.desc()).limit(SEARCH_WINDOW)
        async with self._s.session_factory() as session:
            rows = (await session.execute(query)).tuples().all()
        found: list[ThreadItem] = []
        for thread, message in rows:
            item = self._item(thread, message)
            text = f"{item.subject} {item.sender} {item.sender_address}".casefold()
            if all(word in text for word in wanted):
                found.append(item)
                if len(found) >= limit:
                    break
        return found

    def _select(self) -> Any:
        return select(MailThread, MailMessage).outerjoin(
            MailMessage, MailMessage.id == MailThread.last_inbound_id
        )

    def _item(self, thread: MailThread, message: MailMessage | None) -> ThreadItem:
        store = self._store
        address = message.from_address if message is not None else ""
        names = store.dec_json(message.names_enc, {}) if message is not None else {}
        return ThreadItem(
            id=thread.id,
            subject=store.dec(thread.subject_enc) or "(no subject)",
            sender=str(names.get(address) or address or "you"),
            sender_address=address,
            category=thread.owner_category or thread.category,
            priority=thread.priority,
            needs_reply=thread.needs_reply,
            unread=thread.unread,
            in_inbox=thread.in_inbox,
            summary=store.dec(thread.summary_enc),
            signals=list(thread.signals or []),
            last_message_at=thread.last_message_at,
        )
