"""How well Jarvis sorts your email: `jarvis eval triage` (`make eval`).

Every email whose category you confirmed or corrected in the Inbox ("Is this
right?") is sorted again, with the models configured now, and compared with your
answer. It runs on this computer: emails come from Jarvis's encrypted database
(text Jarvis has forgotten is fetched again from Gmail, as when you open it),
nothing in your inbox or in Jarvis's sorting changes, and the report shows counts
only, never what the emails say. Each model call is audited as usual.
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO

import httpx
from sqlalchemy import select

from jarvis.config import Settings
from jarvis.db.models import MailMessage, MailVerdict
from jarvis.db.session import SessionFactory, create_engine, create_session_factory
from jarvis.ingestion.google import GoogleAuth
from jarvis.llm.router import RouterError
from jarvis.mail.config import load_email_config
from jarvis.mail.service import MailService
from jarvis.mail.triage import CATEGORIES
from jarvis.services import Services, build_services

TARGET = 0.90
ENOUGH = 50  # checked emails before the score says much
NAMES = {
    "urgent": "Urgent",
    "needs_reply": "Needs reply",
    "fyi": "FYI",
    "newsletter": "Newsletter",
    "lead": "Lead",
    "invoice": "Invoice",
    "suspicious": "Suspicious",
}


class EvalError(RuntimeError):
    pass


@dataclass(frozen=True)
class Graded:
    message_id: str
    expected: str  # your category
    got: str  # Jarvis's, now
    then: str | None  # Jarvis's, when the email arrived
    model: str | None  # None: the rules decided without a model


@dataclass
class TriageReport:
    graded: list[Graded] = field(default_factory=list)
    unreadable: int = 0  # emails whose text couldn't be read again
    target: float = TARGET

    @property
    def right(self) -> int:
        return sum(g.got == g.expected for g in self.graded)

    @property
    def accuracy(self) -> float:
        return self.right / len(self.graded) if self.graded else 0.0

    @property
    def accuracy_then(self) -> float | None:
        then = [g for g in self.graded if g.then]
        return sum(g.then == g.expected for g in then) / len(then) if then else None

    @property
    def passed(self) -> bool:
        return len(self.graded) >= ENOUGH and self.accuracy >= self.target

    def render(self) -> str:
        unreadable = (
            f"{self.unreadable} checked emails couldn't be read again and were left out."
            if self.unreadable
            else ""
        )
        if not self.graded:
            return (
                "No checked emails to score yet. In the Inbox, open an email and use "
                "“Is this right?” to confirm or correct its category. Check at least "
                f"{ENOUGH}, then run `make eval` again.\n{unreadable}"
            ).strip()
        models = Counter(g.model or "rules only" for g in self.graded)
        lines = [
            f"Jarvis sorted again the {len(self.graded)} emails you checked, using "
            + ", ".join(f"{name} ({count})" for name, count in models.most_common())
            + ".",
            "",
            f"  {'Category':<13}{'Emails':>7}{'Right':>7}{'Score':>7}  Mistaken for",
        ]
        for category in CATEGORIES:
            mine = [g for g in self.graded if g.expected == category]
            if not mine:
                continue
            right = sum(g.got == category for g in mine)
            wrong = Counter(NAMES[g.got] for g in mine if g.got != category)
            row = f"  {NAMES[category]:<13}{len(mine):>7}{right:>7}{right / len(mine):>7.0%}  "
            row += ", ".join(f"{name} {count}" for name, count in wrong.most_common(3))
            lines.append(row.rstrip())
        lines.append(f"  {'All':<13}{len(self.graded):>7}{self.right:>7}{self.accuracy:>7.0%}")
        lines.append("")
        if self.accuracy_then is not None:
            lines.append(f"When these emails arrived, Jarvis got {self.accuracy_then:.0%} right.")
        if unreadable:
            lines.append(unreadable)
        if len(self.graded) < ENOUGH:
            lines.append(
                f"Only {len(self.graded)} checked emails so far: check at least {ENOUGH} in the "
                "Inbox (“Is this right?”) for a score you can rely on."
            )
        verdict = "passed" if self.passed else "not passed yet"
        lines.append(
            f"Target: {self.target:.0%} on at least {ENOUGH} emails: {verdict} "
            f"({self.accuracy:.0%} on {len(self.graded)})."
        )
        return "\n".join(lines)


async def evaluate_triage(
    services: Services,
    mail: MailService,
    *,
    limit: int = 200,
    progress: Callable[[int, int], None] | None = None,
) -> TriageReport:
    """Sort again your most recently checked emails, and compare with your answers."""
    async with services.session_factory() as session:
        verdicts = list(
            await session.scalars(
                select(MailVerdict).order_by(MailVerdict.decided_at.desc()).limit(limit)
            )
        )
        threads = {v.thread_id for v in verdicts}
        forgotten = list(
            await session.scalars(
                select(MailMessage)
                .where(MailMessage.thread_id.in_(threads))
                .where(MailMessage.body_enc.is_(None))
            )
        )
    await mail.refetch_bodies(forgotten)
    async with services.session_factory() as session:
        readable = set(
            await session.scalars(
                select(MailMessage.id)
                .where(MailMessage.id.in_([v.message_id for v in verdicts]))
                .where(MailMessage.body_enc.is_not(None))
            )
        )
    report = TriageReport()
    for done, verdict in enumerate(verdicts, 1):
        if progress is not None:
            progress(done, len(verdicts))
        if verdict.message_id not in readable:
            report.unreadable += 1
            continue
        try:
            triage = await mail.triage.assess(verdict.thread_id, message_id=verdict.message_id)
        except RouterError as exc:
            raise EvalError(f"No model could sort email just now: {exc}") from exc
        if triage is None:
            report.unreadable += 1
            continue
        report.graded.append(
            Graded(
                message_id=verdict.message_id,
                expected=verdict.category,
                got=triage.category,
                then=verdict.jarvis_category,
                model=triage.model,
            )
        )
    return report


async def run_triage_eval(
    settings: Settings,
    *,
    limit: int = 200,
    out: TextIO = sys.stdout,
    services_factory: Callable[[Settings, SessionFactory], Services] = build_services,
    http_transport: httpx.AsyncBaseTransport | None = None,  # tests: a fake Google
) -> int:
    """`jarvis eval triage`: print the report. Exit code 0 means the target is met."""
    config = load_email_config(settings.email_config_path)
    engine = create_engine(settings.database_url)
    try:
        services = services_factory(settings, create_session_factory(engine))
        async with httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": "Jarvis/0.1"}, transport=http_transport
        ) as http:
            google = GoogleAuth.from_settings(
                settings, vault=services.vault, clock=services.clock, http=http
            )
            mail = MailService(services, google=google, http=http, config=config)

            def progress(done: int, total: int) -> None:
                if done == 1 or done % 10 == 0 or done == total:
                    print(f"Sorting {done}/{total}...", file=sys.stderr, flush=True)

            report = await evaluate_triage(services, mail, limit=limit, progress=progress)
    except EvalError as exc:
        print(exc, file=out)
        return 1
    finally:
        await engine.dispose()
    print(report.render(), file=out)
    return 0 if report.passed else 1
