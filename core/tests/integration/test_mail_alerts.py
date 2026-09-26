"""Alerts and digests: urgent mail from people you know, catching up, and the daily summaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jarvis.db.models import MailMessage, MailThread, SystemState
from jarvis.db.session import transaction
from jarvis.mail.digest import DIGEST_KEY
from jarvis.mail.service import MailService
from jarvis.workflows import scheduler
from tests.integration.mail_helpers import MailRig, Notes, ScriptedMail

pytestmark = pytest.mark.db

ACHIENG = "Achieng Otieno <achieng@client.co.ke>"
URGENT = {
    "category": "urgent",
    "priority": 5,
    "needs_reply": True,
    "summary": "The site is down; asks you to call.",
}


@pytest.fixture
def notes(mailer: MailService) -> Notes:
    channel = Notes()
    mailer.notifier.telegram = channel
    return channel


async def add_contact(mail: MailRig, address: str, *, vip: bool) -> None:
    async with transaction(mail.services.session_factory) as session:
        await mail.services.profiles.update(
            session,
            {
                "people.contacts": [
                    {"name": "Achieng", "email": address, "relationship": "client", "vip": vip}
                ]
            },
            created_by="owner",
        )


async def thread_of(mail: MailRig, message: str) -> MailThread:
    async with mail.services.session_factory() as session:
        row = await session.get(MailThread, mail.fake.messages[message]["threadId"])
    assert row is not None
    return row


# --- Google access ----------------------------------------------------------------------------


async def test_losing_google_access_is_said_once(
    mail: MailRig, mailer: MailService, notes: Notes
) -> None:
    await mailer.sync_round()
    mail.fake.faults.add("revoked")  # you changed your Google password
    for _ in range(3):
        await mailer.sync_round()
    said = [text for text, _ in notes.said if text.startswith("Reconnect Google")]
    assert len(said) == 1
    assert "Connect Google again in Sources" in said[0]

    mail.fake.faults.discard("revoked")  # you reconnected
    await mailer.sync_round()
    mail.fake.faults.add("revoked")  # and it happened again
    await mailer.sync_round()
    assert len([t for t, _ in notes.said if t.startswith("Reconnect Google")]) == 2


async def test_losing_google_access_is_said_until_it_reaches_you(
    mail: MailRig, mailer: MailService, notes: Notes
) -> None:
    await mailer.sync_round()
    mail.fake.faults.add("revoked")
    mailer.notifier.telegram = None  # no Telegram, and no phone signed up for alerts
    await mailer.sync_round()
    await mailer.sync_round()
    assert notes.said == []
    mailer.notifier.telegram = notes  # now there's somewhere to say it
    await mailer.sync_round()
    await mailer.sync_round()
    assert len([t for t, _ in notes.said if t.startswith("Reconnect Google")]) == 1


# --- Urgent alerts ----------------------------------------------------------------------------


async def test_urgent_mail_from_someone_you_know_alerts_you_at_once(
    mail: MailRig, mailer: MailService, notes: Notes
) -> None:
    await add_contact(mail, "achieng@client.co.ke", vip=False)
    ScriptedMail(triage=URGENT).install(mail.services)
    message = mail.fake.deliver(sender=ACHIENG, subject="Site down!")
    await mailer.sync_round()
    assert await mailer.triage_round() == 1

    [(text, silent)] = notes.said
    assert text.startswith(
        "📧 Urgent: Achieng Otieno\n\nSite down!\nThe site is down; asks you to call."
    )
    assert not silent
    assert (await thread_of(mail, message)).alerted_at == mail.clock.now()


async def test_a_vip_waiting_on_you_is_an_alert_too(
    mail: MailRig, mailer: MailService, notes: Notes
) -> None:
    await add_contact(mail, "achieng@client.co.ke", vip=True)
    ScriptedMail(
        triage={"category": "needs_reply", "needs_reply": True, "summary": "Asks for a quote."}
    ).install(mail.services)
    mail.fake.deliver(sender=ACHIENG, subject="Quote?")
    await mailer.sync_round()
    await mailer.triage_round()
    assert [text.split("\n")[0] for text in notes.alerts()] == ["📧 From a VIP: Achieng Otieno"]


async def test_strangers_never_trigger_an_alert(
    mail: MailRig, mailer: MailService, notes: Notes
) -> None:
    ScriptedMail(triage=URGENT).install(mail.services)
    mail.fake.deliver(sender="Stranger <s@unknown.test>", subject="URGENT: wire the money")
    await mailer.sync_round()
    assert await mailer.triage_round() == 1
    assert notes.said == []  # it waits for the digest


async def test_quiet_hours_hold_alerts_back(
    mail: MailRig, mailer: MailService, notes: Notes
) -> None:
    mail.clock.set(datetime(2026, 1, 5, 20, 0, tzinfo=UTC))  # 23:00 in Nairobi
    await add_contact(mail, "achieng@client.co.ke", vip=True)
    ScriptedMail(triage=URGENT).install(mail.services)
    message = mail.fake.deliver(sender=ACHIENG, subject="Site down!")
    await mailer.sync_round()
    await mailer.triage_round()
    assert notes.said == []
    assert (await thread_of(mail, message)).alerted_at is None


async def test_at_most_six_alerts_an_hour(mail: MailRig, mailer: MailService, notes: Notes) -> None:
    await add_contact(mail, "achieng@client.co.ke", vip=False)
    ScriptedMail(triage=URGENT).install(mail.services)
    for n in range(7):
        mail.fake.deliver(sender=ACHIENG, subject=f"Problem {n}")
    await mailer.sync_round()
    assert await mailer.triage_round(limit=10) == 7
    assert len(notes.alerts()) == 6

    mail.clock.advance(minutes=61)
    mail.fake.deliver(sender=ACHIENG, subject="Problem 7")
    await mailer.sync_round()
    await mailer.triage_round()
    assert len(notes.alerts()) == 7


# --- Back online --------------------------------------------------------------------------------


async def test_after_a_gap_one_note_says_how_much_mail_came_in(
    mail: MailRig, mailer: MailService, notes: Notes
) -> None:
    await mailer.sync_round()
    mail.clock.advance(minutes=1)
    mail.fake.deliver(subject="A normal minute")
    await mailer.sync_round()
    assert notes.said == []

    mail.clock.advance(hours=3)  # a power cut
    mail.fake.deliver(subject="One")
    mail.fake.deliver(subject="Two")
    await mailer.sync_round()
    assert notes.said == [
        (
            "Jarvis is back online\n\n"
            "Back after 3 hours away: caught up on 2 emails. Anything urgent comes next.",
            False,
        )
    ]


# --- Digests ------------------------------------------------------------------------------------


async def test_the_digest_sums_up_the_inbox_and_whats_coming(
    mail: MailRig, mailer: MailService, notes: Notes
) -> None:
    ScriptedMail(
        triage={
            "category": "needs_reply",
            "priority": 4,
            "needs_reply": True,
            "summary": "Asks to meet on Tuesday at 10:00.",
            "dates": [{"what": "Kickoff", "start": "2026-01-06T10:00"}],
        }
    ).install(mail.services)
    mail.fake.deliver(sender=ACHIENG, subject="Kickoff next week")
    mail.fake.deliver(
        sender="News <news@letters.test>",
        subject="Weekly digest",
        headers={"List-Unsubscribe": "<mailto:unsubscribe@letters.test>"},
    )
    await mailer.sync_round()
    await mailer.triage_round()

    digest = await mailer.alerts.send_digest("morning", scheduled_for=mail.clock.now())
    assert digest is not None
    assert digest.new_emails == 2
    assert digest.counts["attention"] == 1
    assert digest.counts["newsletters"] == 1
    assert [(i.sender, i.subject) for i in digest.needs_reply] == [
        ("Achieng Otieno", "Kickoff next week")
    ]
    assert [(d.what, d.start) for d in digest.upcoming] == [
        ("Kickoff", "2026-01-06T10:00:00+03:00")
    ]

    [(text, silent)] = notes.said
    assert not silent
    assert text.split("\n") == [
        "☀️ Morning inbox",
        "",
        "2 new emails since Mon 00:00. 2 new · 1 needs attention.",
        "",
        "Needs your attention:",
        "• Achieng Otieno: Kickoff next week (Asks to meet on Tuesday at 10:00.)",
        "",
        "Coming up:",
        "• Tue 06 Jan 10:00: Kickoff (Achieng Otieno)",
    ]

    # Kept for the app, encrypted.
    async with mail.services.session_factory() as session:
        row = await session.get(SystemState, DIGEST_KEY)
    assert row is not None
    assert "Kickoff" not in str(row.value)
    assert await mailer.alerts.latest() == digest


async def test_a_quiet_or_stale_digest_disturbs_nobody(
    mail: MailRig, mailer: MailService, notes: Notes
) -> None:
    await mailer.sync_round()  # connected, nothing in the inbox
    quiet = await mailer.alerts.send_digest("evening", scheduled_for=mail.clock.now())
    assert quiet is not None
    assert quiet.quiet
    assert notes.said == []
    assert await mailer.alerts.latest() == quiet

    late = mail.clock.now() - timedelta(hours=4)  # Jarvis was off at digest time
    assert await mailer.alerts.send_digest("morning", scheduled_for=late) is None


async def test_the_scheduled_jobs_send_digests_and_forget_old_email_text(
    mail: MailRig, mailer: MailService, notes: Notes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler, "_services", mail.services)
    monkeypatch.setattr(scheduler, "_mail", mailer)
    ScriptedMail(triage=URGENT).install(mail.services)
    message = mail.fake.deliver(sender=ACHIENG, subject="Site down!")
    await mailer.sync_round()
    await mailer.triage_round()
    assert await scheduler.run_mail_digest("morning", mail.clock.now())
    assert notes.said[-1][0].startswith("☀️ Morning inbox")

    async def body() -> Any:
        async with mail.services.session_factory() as session:
            row = await session.get(MailMessage, message)
        assert row is not None
        return row.body_enc

    assert (await scheduler.run_nightly_maintenance())["purged_email_bodies"] == 0
    assert await body() is not None
    mail.clock.advance(days=91)  # past the 90 days email.yaml keeps
    assert (await scheduler.run_nightly_maintenance())["purged_email_bodies"] == 1
    assert await body() is None
