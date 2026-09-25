"""Email and calendar actions: what can be proposed, and how each one runs.

Every one of these goes through the policy engine (config/policies.yaml):
`email.send` always waits for your approval, then the undo window; the others
are low risk. The executors run exactly the payload that was approved.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.mail.gmail import SendOutcomeUnknown, SendRefused
from jarvis.mail.mime import build_message, to_raw
from jarvis.policy.registry import ActionRegistry, ActionSpec, ExecutionContext, OutcomeUnknownError

if TYPE_CHECKING:
    from jarvis.mail.service import MailService

log = logging.getLogger("jarvis.mail")

_ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
JARVIS_LABELS = {
    "urgent": "Jarvis/Urgent",
    "needs_reply": "Jarvis/Needs reply",
    "fyi": "Jarvis/FYI",
    "newsletter": "Jarvis/Newsletter",
    "lead": "Jarvis/Lead",
    "invoice": "Jarvis/Invoice",
    "suspicious": "Jarvis/Suspicious",
}
JarvisLabel = Literal[
    "Jarvis/Urgent",
    "Jarvis/Needs reply",
    "Jarvis/FYI",
    "Jarvis/Newsletter",
    "Jarvis/Lead",
    "Jarvis/Invoice",
    "Jarvis/Suspicious",
]


def is_address(value: str) -> bool:
    return bool(_ADDRESS_RE.match(value.strip().lower()))


def _address(value: str) -> str:
    value = value.strip().lower()
    if not is_address(value):
        raise ValueError(f"'{value}' isn't an email address")
    return value


def _one_line(value: str | None) -> str | None:
    if value is not None and ("\r" in value or "\n" in value):
        raise ValueError("must be a single line")
    return value


Address = Annotated[str, AfterValidator(_address)]
OneLine = Annotated[str, AfterValidator(_one_line)]


class _Email(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str | None = Field(default=None, max_length=64)
    to: list[Address] = Field(min_length=1, max_length=20)
    cc: list[Address] = Field(default_factory=list, max_length=20)
    bcc: list[Address] = Field(default_factory=list, max_length=20)
    subject: OneLine = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)
    in_reply_to: OneLine | None = Field(default=None, max_length=1_000)
    references: OneLine | None = Field(default=None, max_length=4_000)
    draft_id: uuid.UUID | None = None  # Jarvis's record of the reply, for bookkeeping

    @model_validator(mode="after")
    def _dedupe(self) -> _Email:
        seen: set[str] = set()
        for field in ("to", "cc", "bcc"):  # an address appears once, in its first field
            kept: list[str] = []
            for address in getattr(self, field):
                if address not in seen:
                    seen.add(address)
                    kept.append(address)
            setattr(self, field, kept)
        if not self.to:
            raise ValueError("needs at least one recipient")
        return self

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        value = value.replace("\r\n", "\n").strip()
        if not value:
            raise ValueError("the message is empty")
        return value

    def mime(self, *, sender: str, sender_name: str | None, action_id: str | None) -> bytes:
        return build_message(
            sender=sender,
            sender_name=sender_name,
            to=self.to,
            cc=self.cc,
            bcc=self.bcc,
            subject=self.subject,
            body=self.body,
            in_reply_to=self.in_reply_to,
            references=self.references,
            action_id=action_id,
        )


class EmailSendPayload(_Email):
    """Send this exact email (a reply when `thread_id` and `in_reply_to` are set)."""


class EmailDraftPayload(_Email):
    """Save this reply in Gmail's Drafts, so you see it there too (nothing is sent)."""

    @model_validator(mode="after")
    def _needs_draft(self) -> EmailDraftPayload:
        if self.draft_id is None:
            raise ValueError("draft_id is required: it names the reply this copy belongs to")
        return self


class EmailLabelPayload(BaseModel):
    """Put one Jarvis label on an email. Only Jarvis/* labels exist here, so this
    can never archive, trash, spam or unlabel anything of yours."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=64)
    label: JarvisLabel


class CalendarHoldPayload(BaseModel):
    """A private hold on your calendar: no invitees, a popup reminder."""

    model_config = ConfigDict(extra="forbid")

    title: OneLine = Field(min_length=1, max_length=200)
    start: str
    end: str | None = None
    all_day: bool = False
    reminder_minutes: int = Field(default=30, ge=0, le=1_440)
    thread_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _times(self) -> CalendarHoldPayload:
        start = _parse_time(self.start, self.all_day)
        if self.end is not None and _parse_time(self.end, self.all_day) <= start:
            raise ValueError("the hold must end after it starts")
        return self

    def event(self) -> dict[str, Any]:
        start = _parse_time(self.start, self.all_day)
        if self.all_day:
            end = _parse_time(self.end, True) if self.end else start + timedelta(days=1)
            times = {"start": {"date": start.isoformat()}, "end": {"date": end.isoformat()}}
        else:
            end = _parse_time(self.end, False) if self.end else start + timedelta(hours=1)
            times = {"start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
        return {
            "summary": self.title,
            "description": "Held by Jarvis, from an email.",
            "visibility": "private",
            "transparency": "opaque",
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": self.reminder_minutes}],
            },
            **times,
        }


def _parse_time(value: str, all_day: bool) -> Any:
    if all_day:
        return date.fromisoformat(value[:10])
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("a time needs its timezone")
    return parsed


def _who(addresses: list[str]) -> str:
    return addresses[0] + (f" and {len(addresses) - 1} more" if len(addresses) > 1 else "")


def register_mail_actions(registry: ActionRegistry, mail: MailService) -> None:
    async def send(ctx: ExecutionContext, payload: EmailSendPayload) -> dict[str, Any]:
        access = await mail.access()
        if access.level != "full" or not access.account:
            raise RuntimeError(
                "Jarvis can't send from your Gmail yet: give it inbox access in Sources."
            )
        raw = payload.mime(
            sender=access.account,
            sender_name=await mail.owner_name(),
            action_id=str(ctx.proposal_id),
        )
        try:
            sent = await mail.client().send(to_raw(raw), payload.thread_id)
        except SendOutcomeUnknown as exc:
            raise OutcomeUnknownError(f"{exc}. Jarvis will look for it in your Sent mail.") from exc
        except SendRefused as exc:
            raise RuntimeError(str(exc)) from exc
        try:  # it's sent: bookkeeping trouble must never turn that into a failure
            await mail.after_send(payload.draft_id, message_id=str(sent.get("id", "")))
        except Exception:
            log.exception("mail: tidying up after a send failed")
        return {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}

    async def save_draft(ctx: ExecutionContext, payload: EmailDraftPayload) -> dict[str, Any]:
        return await mail.mirror_draft(payload)

    async def label(ctx: ExecutionContext, payload: EmailLabelPayload) -> dict[str, Any]:
        client = mail.client()
        ids = await mail.label_ids()
        add = ids[payload.label]
        others = [label_id for name, label_id in ids.items() if name != payload.label]
        await client.modify_message(payload.message_id, add=[add], remove=others)
        return {"label": payload.label}

    async def hold(ctx: ExecutionContext, payload: CalendarHoldPayload) -> dict[str, Any]:
        access = await mail.access()
        if not access.can_add_holds:
            raise RuntimeError(
                "Jarvis can't add calendar holds yet: reconnect Google in Sources and allow "
                "calendar events."
            )
        event = await mail.client().insert_event(payload.event())
        return {"event_id": event.get("id"), "link": event.get("htmlLink")}

    registry.register(
        ActionSpec(
            kind="email.send",
            payload_model=EmailSendPayload,
            executor=send,
            summarize=lambda p: (
                f"{'Reply to' if p.in_reply_to else 'Email'} {_who(p.to)}: {p.subject}"
            ),
            timeout_seconds=60,
        )
    )
    registry.register(
        ActionSpec(
            kind="email.draft",
            payload_model=EmailDraftPayload,
            executor=save_draft,
            summarize=lambda p: f"Save a draft to {_who(p.to)} in Gmail: {p.subject}",
            timeout_seconds=60,
        )
    )
    registry.register(
        ActionSpec(
            kind="email.label",
            payload_model=EmailLabelPayload,
            executor=label,
            summarize=lambda p: f"Label an email in Gmail: {p.label}",
            timeout_seconds=30,
        )
    )
    registry.register(
        ActionSpec(
            kind="calendar.hold",
            payload_model=CalendarHoldPayload,
            executor=hold,
            summarize=lambda p: f"Hold '{p.title}' in your calendar ({p.start[:16]})",
            timeout_seconds=30,
        )
    )
