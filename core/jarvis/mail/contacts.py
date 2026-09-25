"""Who counts as someone you know, for the recipients check on every email.

* **Known:** your profile's contacts, anyone you've emailed before (as synced
  from Gmail), and you.
* **On the conversation:** for a reply, the people who wrote it or were
  addressed in it (From, To and Cc of its emails) are fine too. An address that
  only ever appeared as a Reply-To doesn't count: redirecting replies that way
  is a classic trick, so Jarvis flags it for a careful look.

Anyone else is a "new recipient": the email can still be sent, but never
without you seeing that warning first.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import MailContact, MailMessage, OAuthToken
from jarvis.profile.service import ProfileService


class MailContacts:
    def __init__(self, profiles: ProfileService) -> None:
        self._profiles = profiles

    async def is_known(self, session: AsyncSession, address: str) -> bool:
        address = address.strip().lower()
        if await self._profiles.is_known(session, address):
            return True
        contact = await session.get(MailContact, address)
        if contact is not None and contact.sent_count > 0:
            return True
        yours = await session.scalars(
            select(OAuthToken.account_email).where(OAuthToken.account_email.is_not(None))
        )
        return address in {a.strip().lower() for a in yours if a}

    async def thread_participants(self, session: AsyncSession, thread_id: str) -> set[str]:
        rows = await session.execute(
            select(MailMessage.from_address, MailMessage.to_addresses, MailMessage.cc_addresses)
            .where(MailMessage.thread_id == thread_id)
            .where(MailMessage.deleted.is_(False))
        )
        people: set[str] = set()
        for sender, to, cc in rows:
            people.update(a.strip().lower() for a in (sender, *to, *cc) if a)
        return people
