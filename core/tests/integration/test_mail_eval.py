"""`make eval`: Jarvis sorts again the emails you checked, and is scored against your answers."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import func, select

from jarvis.config import Settings
from jarvis.db.models import ActionProposal, MailThread, MailVerdict
from jarvis.db.session import SessionFactory
from jarvis.mail.evaluate import evaluate_triage, run_triage_eval
from jarvis.mail.service import MailService
from jarvis.memory.embeddings import HashEmbedder
from jarvis.services import Services, build_services
from tests.conftest import fake_models_config
from tests.integration.mail_helpers import MailRig, ScriptedMail

pytestmark = pytest.mark.db


@dataclass
class Sorter:
    """A triage model that calls anything about a website a lead."""

    asked: int = 0

    def install(self, services: Services) -> Sorter:
        services.router._models["fake-local"] = FunctionModel(self)
        return self

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.asked += 1
        category = "lead" if "website" in str(messages[-1]).lower() else "needs_reply"
        output = info.output_tools[0]
        return ModelResponse(parts=[ToolCallPart(output.name, {"category": category})])


async def checked_emails(mail: MailRig, mailer: MailService) -> dict[str, str]:
    """Four emails Jarvis sorted as they arrived, then you checked in the Inbox."""
    ScriptedMail().install(mail.services)  # says "needs reply" to everything
    emails = {
        "kickoff": ({"sender": "Achieng Otieno <achieng@client.co.ke>"}, "needs_reply"),
        "website": (
            {"sender": "Wanjiru <wanjiru@startup.test>", "subject": "New website?"},
            "lead",
        ),
        "newsletter": (
            {
                "sender": "Tech Weekly <news@weekly.test>",
                "subject": "This week in tech",
                "headers": {"List-Unsubscribe": "<mailto:u@weekly.test>"},
            },
            "newsletter",
        ),
        "invoice": (
            {"sender": "Billing <billing@vendor.test>", "subject": "Invoice 4471"},
            "invoice",
        ),
    }
    ids = {name: mail.fake.deliver(**email) for name, (email, _) in emails.items()}
    await mailer.sync_round()
    await mailer.triage_round()
    for name, (_, yours) in emails.items():
        await mailer.record_verdict(str(mail.fake.messages[ids[name]]["threadId"]), yours)
    return ids


async def sorting_state(sf: SessionFactory) -> list[tuple[Any, ...]]:
    async with sf() as session:
        rows = await session.scalars(select(MailThread).order_by(MailThread.id))
        return [
            (t.id, t.category, t.priority, t.needs_reply, t.summary_enc, t.signals, t.triaged_at)
            for t in rows
        ]


def gmail_changes(mail: MailRig) -> list[tuple[str, str]]:
    return [(m, p) for m, p in mail.fake.requests if m != "GET" and "/gmail/" in p]


async def test_jarvis_is_scored_against_what_you_checked(
    mail: MailRig, mailer: MailService
) -> None:
    await checked_emails(mail, mailer)
    services = mail.services
    before, changes = await sorting_state(services.session_factory), gmail_changes(mail)
    sorter = Sorter().install(services)

    report = await evaluate_triage(services, mailer)

    got = {g.expected: (g.got, g.then, g.model) for g in report.graded}
    assert got == {
        "needs_reply": ("needs_reply", "needs_reply", "fake-local"),
        "lead": ("lead", "needs_reply", "fake-local"),  # the model now gets it right
        "newsletter": ("newsletter", "newsletter", None),  # rules alone, no model
        "invoice": ("needs_reply", "needs_reply", "fake-local"),  # still wrong
    }
    assert sorter.asked == 3
    assert (report.right, report.accuracy, report.accuracy_then) == (3, 0.75, 0.5)
    assert not report.passed  # 75%, and far fewer than 50 emails

    text = report.render()
    assert "Invoice            1      0     0%  Needs reply 1" in text
    assert "All                4      3    75%" in text
    assert "When these emails arrived, Jarvis got 50% right." in text
    assert "Only 4 checked emails so far" in text
    assert "not passed yet (75% on 4)" in text

    # Scoring changes nothing: not the sorting, not your Gmail, no actions.
    assert await sorting_state(services.session_factory) == before
    assert gmail_changes(mail) == changes
    async with services.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ActionProposal)) == 0
        assert await session.scalar(select(func.count()).select_from(MailVerdict)) == 4


async def test_forgotten_text_is_fetched_again_and_deleted_email_is_left_out(
    mail: MailRig, mailer: MailService
) -> None:
    ids = await checked_emails(mail, mailer)
    mail.clock.advance(days=91)
    assert await mailer.purge_old_bodies() == 4
    mail.fake.messages.pop(ids["invoice"])  # gone from Gmail too
    Sorter().install(mail.services)

    report = await evaluate_triage(mail.services, mailer)
    assert sorted(g.expected for g in report.graded) == ["lead", "needs_reply", "newsletter"]
    assert report.unreadable == 1
    assert "1 checked emails couldn't be read again and were left out." in report.render()


async def test_the_command_prints_the_report(mail: MailRig, mailer: MailService) -> None:
    def fresh(settings: Settings, sf: SessionFactory) -> Services:
        """What `jarvis eval triage` builds for itself (with the test model and clock)."""
        services = build_services(
            settings,
            sf,
            clock=mail.clock,
            embedder=HashEmbedder(),
            models_config=fake_models_config(),
        )
        Sorter().install(services)
        return services

    out = io.StringIO()
    assert await run_triage_eval(mail.services.settings, out=out, services_factory=fresh) == 1
    assert out.getvalue().startswith("No checked emails to score yet.")

    await checked_emails(mail, mailer)
    out = io.StringIO()
    code = await run_triage_eval(mail.services.settings, out=out, services_factory=fresh)
    assert code == 1  # not enough checked emails yet
    assert "Jarvis sorted again the 4 emails you checked, using fake-local (3)" in out.getvalue()
    assert "All                4      3    75%" in out.getvalue()
