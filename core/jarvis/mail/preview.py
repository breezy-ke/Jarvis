"""A short preview of an email action (who, subject, what it says) for voice and Telegram.

Built from the action's payload, never from a model's description of it, so what
you hear or read before approving is what would be sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

EMAIL_KINDS = frozenset({"email.send", "email.draft"})
FIRST_LINE_CHARS = 160
# "Hi Achieng," says nothing about the reply: preview the line after it.
_GREETING_RE = re.compile(
    r"^(hi|hello|hey|dear|habari|hujambo|salaam|good (morning|afternoon|evening))\b"
    r"[^.!?]{0,40}[,!]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EmailPreview:
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    first_line: str


def first_line(body: str) -> str:
    lines = [line.strip() for line in body.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        return ""
    line = next((item for item in lines if not _GREETING_RE.match(item)), lines[0])
    if len(line) > FIRST_LINE_CHARS:
        line = line[: FIRST_LINE_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return line


def email_preview(kind: str, payload: dict[str, Any] | None) -> EmailPreview | None:
    if kind not in EMAIL_KINDS or not payload:
        return None
    return EmailPreview(
        to=[str(a) for a in payload.get("to") or []],
        cc=[str(a) for a in payload.get("cc") or []],
        bcc=[str(a) for a in payload.get("bcc") or []],
        subject=str(payload.get("subject") or ""),
        first_line=first_line(str(payload.get("body") or "")),
    )
