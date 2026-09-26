"""The Inbox API: connect Google, read sorted mail, draft, send with undo, correct, hold."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from jarvis.api.app import create_app
from jarvis.clock import FrozenClock
from jarvis.db.models import MailVerdict
from jarvis.db.session import SessionFactory
from jarvis.mail.service import MailService
from jarvis.memory.embeddings import HashEmbedder
from jarvis.services import Services, build_services
from tests.conftest import fake_models_config, make_settings
from tests.fake_gmail import FakeGmail
from tests.integration.api_helpers import ORIGIN, Harness, login, register
from tests.integration.mail_helpers import OWNER, ScriptedMail
from tests.webauthn_helpers import SoftAuthenticator

pytestmark = pytest.mark.db

ACHIENG = "Achieng Otieno <achieng@client.co.ke>"


@dataclass
class Inbox:
    h: Harness
    fake: FakeGmail

    @property
    def mail(self) -> MailService:
        assert self.h.state.mail is not None
        return self.h.state.mail

    @property
    def services(self) -> Services:
        return self.h.services

    async def get(self, path: str) -> Any:
        resp = await self.h.client.get(path)
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = await self.h.client.post(path, json=body or {})
        assert resp.status_code in (200, 202), resp.text
        return resp.json()

    async def settle(self) -> None:
        """What the background loops would do: sync, then sort."""
        await self.mail.sync_round()
        await self.mail.triage_round()


@pytest.fixture
async def inbox(session_factory: SessionFactory, clock: FrozenClock) -> AsyncIterator[Inbox]:
    fake = FakeGmail(account=OWNER, now=clock.now)
    settings = make_settings(
        JARVIS_ENABLE_SCHEDULER=False,
        JARVIS_GOOGLE_FAKE_BASE="http://google.test",
        GOOGLE_OAUTH_CLIENT_ID="fake-client",
        GOOGLE_OAUTH_CLIENT_SECRET="fake-secret",
    )

    def factory(s: Any, sf: SessionFactory) -> Services:
        return build_services(
            s, sf, clock=clock, embedder=HashEmbedder(), models_config=fake_models_config()
        )

    app = create_app(
        settings, services_factory=factory, run_background=False, http_transport=fake.transport()
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client,
    ):
        harness = Harness(
            app=app,
            client=client,
            state=app.state.jarvis,
            authenticator=SoftAuthenticator(rp_id="localhost", origin=ORIGIN),
            clock=clock,
        )
        await register(harness)
        await login(harness)
        yield Inbox(harness, fake)


async def connect_google(inbox: Inbox) -> None:
    """Sources → Connect Google → Google's consent screen → back to Jarvis."""
    start = await inbox.post("/api/integrations/google/connect")
    async with httpx.AsyncClient(transport=inbox.fake.transport()) as browser:
        consent = await browser.get(start["auth_url"])
    back = httpx.URL(consent.headers["location"])
    page = await inbox.h.client.get("/api/integrations/google/callback", params=dict(back.params))
    assert page.status_code == 200, page.text
    assert "Nothing is sent without your approval" in page.text


async def test_connecting_google_gives_jarvis_your_inbox(inbox: Inbox) -> None:
    before = await inbox.get("/api/integrations/google")
    assert before["connected"] is False
    assert (await inbox.get("/api/mail/status"))["access"] == "none"

    await connect_google(inbox)
    google = await inbox.get("/api/integrations/google")
    assert google["mail_access"] == "full"
    assert google["can_add_holds"] is True
    status = await inbox.get("/api/mail/status")
    assert status["access"] == "full"
    assert status["account"] == OWNER
    assert status["rules"] == {"auto_draft": "known", "alerts": "known"}


async def test_reading_sorted_mail_as_plain_text(inbox: Inbox) -> None:
    await connect_google(inbox)
    ScriptedMail().install(inbox.services)
    message = inbox.fake.deliver(
        sender=ACHIENG,
        subject="Kickoff next week",
        html=(
            "<p>Can we meet on <b>Tuesday</b>?</p>"
            '<img src="https://tracker.example/pixel.gif"><script>alert(1)</script>'
        ),
    )
    await inbox.settle()
    thread_id = inbox.fake.messages[message]["threadId"]

    status = await inbox.get("/api/mail/status")
    assert status["counts"]["attention"] == 1
    assert status["sync"]["status"] == "ok"
    [item] = await inbox.get("/api/mail/threads?tab=attention")
    assert item["id"] == thread_id
    assert item["sender"] == "Achieng Otieno"
    assert item["category"] == "needs_reply"
    assert await inbox.get("/api/mail/threads?tab=newsletters") == []

    view = await inbox.get(f"/api/mail/threads/{thread_id}")
    [shown] = view["messages"]
    assert "Can we meet on Tuesday?" in shown["body"]
    assert "<" not in shown["body"]  # no markup, no remote images, no scripts
    assert "tracker.example" not in shown["body"]
    assert view["draft"] is None
    assert view["checked"] is False

    missing = await inbox.h.client.get("/api/mail/threads/nope")
    assert missing.status_code == 404


async def test_draft_edit_send_undo_and_send_again(inbox: Inbox) -> None:
    await connect_google(inbox)
    model = ScriptedMail().install(inbox.services)
    message = inbox.fake.deliver(sender=ACHIENG, subject="Kickoff next week")
    await inbox.settle()
    thread_id = inbox.fake.messages[message]["threadId"]

    draft = await inbox.post(f"/api/mail/threads/{thread_id}/draft", {"instructions": "yes"})
    assert draft["fields"]["to"] == ["achieng@client.co.ke"]
    assert draft["fields"]["subject"] == "Re: Kickoff next week"
    assert draft["fields"]["body"] == model.reply
    assert draft["proposal"]["status"] == "pending"

    edited = (
        await inbox.h.client.patch(
            f"/api/mail/drafts/{draft['id']}", json={"body": "Tuesday works. See you then."}
        )
    ).json()
    assert edited["fields"]["body"] == "Tuesday works. See you then."
    assert edited["original_body"] == model.reply
    assert edited["proposal"]["id"] != draft["proposal"]["id"]

    # The approval shows the email it answers, and what Jarvis had written.
    [action] = [a for a in await inbox.get("/api/actions?view=open") if a["kind"] == "email.send"]
    assert action["email"]["replying_to"]["sender"] == "Achieng Otieno"
    assert action["email"]["replying_to"]["subject"] == "Kickoff next week"
    assert action["email"]["original_body"] == model.reply

    stale = await inbox.h.client.post(
        f"/api/mail/drafts/{draft['id']}/send",
        json={"payload_hash": draft["proposal"]["payload_hash"]},
    )
    assert stale.status_code == 409  # that version was replaced by your edit

    sent = await inbox.post(
        f"/api/mail/drafts/{draft['id']}/send", {"payload_hash": edited["proposal"]["payload_hash"]}
    )
    assert sent["proposal"]["status"] == "approved"
    undone = await inbox.post(f"/api/mail/drafts/{draft['id']}/undo")
    assert undone["proposal"]["status"] == "cancelled"
    inbox.h.clock.advance(minutes=2)
    await inbox.services.policy.execute_due(inbox.services.session_factory)
    assert inbox.fake.sent() == []

    again = await inbox.post(
        f"/api/mail/drafts/{draft['id']}/send", {"payload_hash": edited["proposal"]["payload_hash"]}
    )
    inbox.h.clock.advance(seconds=61)
    await inbox.services.policy.execute_due(inbox.services.session_factory)
    [email] = inbox.fake.sent()
    assert email["X-Jarvis-Action"] == again["proposal"]["id"]
    view = await inbox.get(f"/api/mail/threads/{thread_id}")
    assert view["draft"] is None  # sent


async def test_is_this_right_is_kept_for_the_accuracy_check(inbox: Inbox) -> None:
    await connect_google(inbox)
    ScriptedMail().install(inbox.services)
    message = inbox.fake.deliver(sender="Wanjiru <wanjiru@startup.test>", subject="New website?")
    await inbox.settle()
    thread_id = inbox.fake.messages[message]["threadId"]

    view = await inbox.post(f"/api/mail/threads/{thread_id}/category", {"category": "lead"})
    assert view["category"] == "lead"
    assert view["jarvis_category"] == "needs_reply"
    assert view["checked"] is True
    assert [t["id"] for t in await inbox.get("/api/mail/threads?tab=leads")] == [thread_id]
    async with inbox.services.session_factory() as session:
        [verdict] = list(await session.scalars(select(MailVerdict)))
    assert (verdict.message_id, verdict.category, verdict.jarvis_category) == (
        message,
        "lead",
        "needs_reply",
    )
    bad = await inbox.h.client.post(
        f"/api/mail/threads/{thread_id}/category", json={"category": "spam"}
    )
    assert bad.status_code == 422


async def test_add_to_calendar_is_a_private_hold(inbox: Inbox) -> None:
    await connect_google(inbox)
    hold = await inbox.post(
        "/api/mail/holds",
        {"title": "Kickoff with Achieng", "start": "2026-01-06T10:00:00+03:00"},
    )
    assert hold["kind"] == "calendar.hold"
    assert hold["status"] == "approved"  # your tap was the approval
    await inbox.services.policy.execute_due(inbox.services.session_factory)
    [event] = inbox.fake.events
    assert event["summary"] == "Kickoff with Achieng"
    assert event["visibility"] == "private"


async def test_forgotten_email_text_comes_back_from_gmail(inbox: Inbox) -> None:
    await connect_google(inbox)
    ScriptedMail().install(inbox.services)
    message = inbox.fake.deliver(sender=ACHIENG, body="The original words.")
    await inbox.settle()
    inbox.h.clock.advance(days=91)
    assert await inbox.mail.purge_old_bodies() == 1
    await login(inbox.h)  # three months on, you sign in again
    view = await inbox.get(f"/api/mail/threads/{inbox.fake.messages[message]['threadId']}")
    assert view["messages"][0]["body"] == "The original words."


async def test_the_inbox_needs_you_signed_in(inbox: Inbox) -> None:
    await inbox.h.client.post("/api/auth/logout")
    for path in ("/api/mail/status", "/api/mail/threads", "/api/mail/threads/x"):
        assert (await inbox.h.client.get(path)).status_code == 401
