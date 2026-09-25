"""Keeping Jarvis's copy of the mailbox in step with Gmail (against a fake Gmail)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from jarvis.db.models import MailContact, MailMessage, MailThread
from jarvis.mail.gmail import GmailAuthError
from tests.integration.mail_helpers import OWNER, MailRig

pytestmark = pytest.mark.db


async def thread(rig: MailRig, thread_id: str) -> MailThread:
    async with rig.services.session_factory() as session:
        row = await session.get(MailThread, thread_id)
    assert row is not None
    return row


async def test_the_first_sync_reads_recent_mail(mail: MailRig) -> None:
    old = mail.clock.advance(days=-30)
    mail.fake.deliver(subject="Ancient history", when=old)
    mail.clock.advance(days=30)
    first = mail.fake.deliver(subject="Kickoff next week")
    mail.fake.deliver(
        sender="News <news@list.example>",
        subject="Weekly digest",
        headers={"List-Unsubscribe": "<mailto:u@list.example>"},
    )
    mail.fake.owner_sends(
        thread_id=mail.fake.messages[first]["threadId"],
        to="achieng@client.co.ke",
        subject="Re: Kickoff next week",
        body="Yes!",
    )

    report = await mail.sync.sync_once()
    assert report is not None
    assert report.full
    assert len(report.stored) == 3  # the 30-day-old message is outside the 14-day window
    assert len(report.new_inbound) == 2
    assert len(report.new_outbound) == 1

    kickoff = await thread(mail, mail.fake.messages[first]["threadId"])
    assert mail.store.dec(kickoff.subject_enc) == "Kickoff next week"
    assert kickoff.in_inbox
    assert kickoff.unread
    assert kickoff.participants == ["achieng@client.co.ke", OWNER]
    assert kickoff.last_inbound_id == first
    assert kickoff.replied_at is not None

    async with mail.services.session_factory() as session:
        achieng = await session.get(MailContact, "achieng@client.co.ke")
        news = await session.get(MailContact, "news@list.example")
        assert await session.get(MailContact, OWNER) is None  # you aren't your own contact
    assert achieng is not None
    assert (achieng.sent_count, achieng.received_count) == (1, 1)
    assert news is not None
    assert (news.sent_count, news.received_count) == (0, 1)

    state = await mail.sync.state()
    assert state.status == "ok"
    assert state.account == OWNER
    assert state.history_id == mail.fake.history_id


async def test_what_emails_say_is_encrypted_at_rest(mail: MailRig) -> None:
    mail.fake.deliver(subject="Quarterly numbers", body="Revenue grew 40 percent in Kisumu.")
    await mail.sync.sync_once()
    async with mail.services.session_factory() as session:
        raw = (await session.execute(text("SELECT subject_enc, body_enc FROM mail_messages"))).one()
        stored = await session.scalar(select(MailMessage))
    assert b"Quarterly" not in raw.subject_enc
    assert b"Revenue" not in raw.body_enc
    assert stored is not None
    assert mail.store.dec(stored.body_enc) == "Revenue grew 40 percent in Kisumu."
    assert stored.from_address == "achieng@client.co.ke"  # addresses stay searchable


async def test_changes_arrive_every_round(mail: MailRig) -> None:
    first = mail.fake.deliver()
    await mail.sync.sync_once()
    thread_id = mail.fake.messages[first]["threadId"]

    second = mail.fake.deliver(subject="Re: Kickoff next week", reply_to_message=first)
    mail.fake.relabel(first, remove=["UNREAD"])  # you read it in Gmail
    news = mail.fake.deliver(sender="news@list.example", subject="Offers")
    mail.fake.deliver(subject="A draft of yours", labels=["DRAFT"])  # drafts aren't mail yet
    report = await mail.sync.sync_once()
    assert report is not None
    assert not report.full
    assert {s.message_id for s in report.new_inbound} == {second, news}
    kickoff = await thread(mail, thread_id)
    assert kickoff.last_inbound_id == second
    assert kickoff.unread  # the new reply is unread

    mail.fake.relabel(second, remove=["UNREAD"])
    mail.fake.relabel(first, remove=["INBOX"])
    mail.fake.relabel(second, remove=["INBOX"])  # archived in Gmail
    mail.fake.delete(news)
    await mail.sync.sync_once()
    kickoff = await thread(mail, thread_id)
    assert not kickoff.in_inbox
    assert not kickoff.unread
    async with mail.services.session_factory() as session:
        deleted = await session.get(MailMessage, news)
    assert deleted is not None
    assert deleted.deleted
    assert (await mail.sync.state()).history_id == mail.fake.history_id


async def test_an_expired_sync_point_means_a_full_sync(mail: MailRig) -> None:
    mail.fake.deliver(subject="Before")
    await mail.sync.sync_once()
    mail.fake.deliver(subject="While Jarvis was off")
    mail.fake.expire_history()
    report = await mail.sync.sync_once()
    assert report is not None
    assert report.full
    assert report.resynced
    assert len(report.new_inbound) == 1  # the one it hadn't seen; the other is refreshed
    async with mail.services.session_factory() as session:
        assert len(list(await session.scalars(select(MailMessage)))) == 2


async def test_gmail_being_busy_is_retried(mail: MailRig) -> None:
    mail.fake.deliver()
    mail.fake.faults.add("rate_limit_once")
    report = await mail.sync.sync_once()
    assert report is not None
    assert len(report.stored) == 1


async def test_revoked_access_asks_you_to_reconnect(mail: MailRig) -> None:
    await mail.sync.sync_once()
    mail.fake.faults.add("revoked")
    with pytest.raises(GmailAuthError):
        await mail.sync.sync_once()
    state = await mail.sync.state()
    assert state.status == "reconnect"
    assert "Reconnect Google" in (state.error or "")


async def test_nothing_happens_without_gmail_access(mail: MailRig) -> None:
    mail.access = type(mail.access)("none", None)
    assert await mail.sync.sync_once() is None
    assert (await mail.sync.state()).status == "off"
    assert mail.fake.requests == []


async def test_a_different_account_starts_afresh(mail: MailRig) -> None:
    mail.fake.deliver()
    await mail.sync.sync_once()
    mail.fake.account = "someone.else@example.com"
    mail.access = type(mail.access)("full", "someone.else@example.com")
    mail.fake.messages.clear()
    await mail.sync.sync_once()
    async with mail.services.session_factory() as session:
        assert await session.scalar(select(MailThread)) is None
        assert await session.scalar(select(MailContact)) is None
    assert (await mail.sync.state()).account == "someone.else@example.com"


async def test_old_email_text_is_purged_but_the_thread_stays(mail: MailRig) -> None:
    mail.fake.deliver(body="Old news")
    await mail.sync.sync_once()
    mail.clock.advance(days=100)
    async with mail.services.session_factory() as session, session.begin():
        cutoff = mail.clock.now() - timedelta(days=90)
        purged = await mail.store.purge_bodies(session, older_than=cutoff)
    assert purged == 1
    async with mail.services.session_factory() as session:
        message = await session.scalar(select(MailMessage))
    assert message is not None
    assert message.body_enc is None
    assert message.subject_enc is not None
