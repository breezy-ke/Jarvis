"""Saving mail in Jarvis's database: content encrypted, metadata plain.

Anything that carries what an email *says* (subject, names, snippet, body,
attachment names, Jarvis's summary) is encrypted with the Vault. Addresses,
dates and labels stay plain, so contacts and filters work without decrypting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.clock import Clock
from jarvis.db.models import MailContact, MailMessage, MailThread
from jarvis.mail.mime import ParsedMessage, plain_differs, thread_subject
from jarvis.security.crypto import Vault

INBOX, UNREAD, SENT, DRAFT, CHAT = "INBOX", "UNREAD", "SENT", "DRAFT", "CHAT"


@dataclass(frozen=True)
class Stored:
    message_id: str
    thread_id: str
    new: bool
    direction: str
    jarvis_action: str | None


class MailStore:
    def __init__(self, vault: Vault, clock: Clock) -> None:
        self._vault = vault
        self._clock = clock

    # --- Encryption ---------------------------------------------------------------

    def enc(self, text: str | None) -> bytes | None:
        return None if text is None else self._vault.encrypt(text)

    def dec(self, blob: bytes | None) -> str:
        return "" if blob is None else self._vault.decrypt_str(blob)

    def enc_json(self, value: Any) -> bytes:
        return self._vault.encrypt(json.dumps(value, ensure_ascii=False))

    def dec_json(self, blob: bytes | None, default: Any) -> Any:
        return default if blob is None else json.loads(self._vault.decrypt_str(blob))

    # --- Messages ------------------------------------------------------------------

    async def upsert(self, session: AsyncSession, parsed: ParsedMessage, *, owner: str) -> Stored:
        """Store a message (or refresh its labels). Contacts are counted once per message."""
        direction = "out" if SENT in parsed.label_ids else "in"
        existing = await session.get(MailMessage, parsed.id, with_for_update=True)
        if existing is not None:
            existing.label_ids = parsed.label_ids
            existing.history_id = max(existing.history_id, parsed.history_id)
            existing.deleted = False
            if existing.body_enc is None and parsed.body:  # purged earlier, fetched again
                existing.body_enc = self.enc(parsed.body)
            return Stored(parsed.id, existing.thread_id, False, direction, parsed.jarvis_action)
        now = self._clock.now()
        if await session.get(MailThread, parsed.thread_id) is None:
            session.add(
                MailThread(
                    id=parsed.thread_id,
                    participants=[],
                    last_message_at=parsed.internal_date,
                    signals=[],
                    updated_at=now,
                )
            )
            await session.flush()
        session.add(
            MailMessage(
                id=parsed.id,
                thread_id=parsed.thread_id,
                history_id=parsed.history_id,
                internal_date=parsed.internal_date,
                direction=direction,
                label_ids=parsed.label_ids,
                from_address=parsed.from_address[:320],
                to_addresses=[a for _, a in parsed.to],
                cc_addresses=[a for _, a in parsed.cc],
                reply_to=[a for _, a in parsed.reply_to],
                names_enc=self.enc_json(parsed.names) if parsed.names else None,
                subject_enc=self.enc(parsed.subject),
                snippet_enc=self.enc(parsed.snippet),
                body_enc=self.enc(parsed.body),
                attachments_enc=self.enc_json(parsed.attachments) if parsed.attachments else None,
                has_attachments=bool(parsed.attachments),
                message_id_header=parsed.message_id,
                references=parsed.references,
                meta={
                    "list_unsubscribe": parsed.list_unsubscribe,
                    "precedence": parsed.precedence,
                    "auth": parsed.auth,
                    "hidden_text": parsed.hidden_text,
                    "in_reply_to": parsed.in_reply_to,
                    "jarvis_action": parsed.jarvis_action,
                    "plain_differs": bool(parsed.plain_text)
                    and plain_differs(parsed.plain_text, parsed.body),
                    "concealed": list(parsed.concealed_signals),
                    "links": parsed.links,
                },
                size=parsed.size,
                fetched_at=now,
            )
        )
        owner = owner.lower()
        if direction == "out":
            for address in {a for _, a in (*parsed.to, *parsed.cc)} - {owner}:
                await self._count(session, address, parsed.internal_date, sent=1)
        elif parsed.from_address and parsed.from_address != owner:
            await self._count(session, parsed.from_address, parsed.internal_date, received=1)
        return Stored(parsed.id, parsed.thread_id, True, direction, parsed.jarvis_action)

    async def _count(
        self,
        session: AsyncSession,
        address: str,
        when: datetime,
        *,
        sent: int = 0,
        received: int = 0,
    ) -> None:
        contact = await session.get(MailContact, address, with_for_update=True)
        if contact is None:
            session.add(
                MailContact(
                    address=address,
                    first_seen=when,
                    last_seen=when,
                    sent_count=sent,
                    received_count=received,
                )
            )
            await session.flush()
            return
        contact.sent_count += sent
        contact.received_count += received
        contact.first_seen = min(contact.first_seen, when)
        contact.last_seen = max(contact.last_seen, when)

    async def set_labels(
        self, session: AsyncSession, message_id: str, labels: list[str]
    ) -> str | None:
        message = await session.get(MailMessage, message_id, with_for_update=True)
        if message is None:
            return None
        message.label_ids = labels
        return message.thread_id

    async def mark_deleted(self, session: AsyncSession, message_id: str) -> str | None:
        message = await session.get(MailMessage, message_id, with_for_update=True)
        if message is None:
            return None
        message.deleted = True
        return message.thread_id

    # --- Threads ---------------------------------------------------------------------

    async def refresh_thread(self, session: AsyncSession, thread_id: str) -> MailThread | None:
        """Recompute a thread's roll-up (participants, inbox, unread, latest messages)."""
        thread = await session.get(MailThread, thread_id, with_for_update=True)
        if thread is None:
            return None
        messages = list(
            await session.scalars(
                select(MailMessage)
                .where(MailMessage.thread_id == thread_id)
                .where(MailMessage.deleted.is_(False))
                .order_by(MailMessage.internal_date, MailMessage.id)
            )
        )
        thread.updated_at = self._clock.now()
        if not messages:
            thread.in_inbox = False
            thread.unread = False
            return thread
        participants: list[str] = []
        for message in messages:
            for address in (message.from_address, *message.to_addresses, *message.cc_addresses):
                if address and address not in participants:
                    participants.append(address)
        thread.participants = participants
        thread.last_message_at = messages[-1].internal_date
        inbound = [m for m in messages if m.direction == "in"]
        outbound = [m for m in messages if m.direction == "out"]
        thread.last_inbound_id = inbound[-1].id if inbound else None
        thread.in_inbox = any(INBOX in m.label_ids for m in messages)
        thread.unread = any(UNREAD in m.label_ids for m in messages)
        thread.subject_enc = self.enc(thread_subject(self.dec(messages[0].subject_enc)))
        thread.replied_at = outbound[-1].internal_date if outbound else None
        if inbound and outbound and outbound[-1].internal_date > inbound[-1].internal_date:
            thread.needs_reply = False  # you've answered the latest message
        return thread

    async def purge_bodies(self, session: AsyncSession, *, older_than: datetime) -> int:
        """Drop stored email text past the retention window (Gmail keeps it)."""
        result = await session.execute(
            update(MailMessage)
            .where(MailMessage.internal_date < older_than)
            .where(MailMessage.body_enc.is_not(None))
            .values(body_enc=None, snippet_enc=None)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
