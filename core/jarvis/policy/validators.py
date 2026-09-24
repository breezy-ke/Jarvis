"""Deterministic checks that run on every proposed action.

Each check returns one of three outcomes:
  * pass  - fine
  * warn  - needs a human look. An L3 action with any warning drops to L2.
  * block - the action is refused and cannot be approved.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from email.utils import getaddresses
from typing import Any, Protocol
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.policy.config import ActionKindPolicy
from jarvis.policy.types import ValidationResult
from jarvis.security.scanners import iter_strings, scan_payload


class KnownContacts(Protocol):
    async def is_known(self, session: AsyncSession, address: str) -> bool: ...


class NoKnownContacts:
    async def is_known(self, session: AsyncSession, address: str) -> bool:
        return False


@dataclass
class ValidationContext:
    kind: str
    payload: dict[str, Any]
    policy: ActionKindPolicy
    session: AsyncSession
    now: datetime
    contacts: KnownContacts


ValidatorFn = Callable[[ValidationContext], Awaitable[ValidationResult]]

_RECIPIENT_FIELDS = ("to", "cc", "bcc", "attendees", "recipients")
_TEXT_FIELDS = ("body", "text", "html", "message", "content")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_URL_RE = re.compile(r"""(?i)\b((?:https?|ftp|javascript|data|vbscript|file):[^\s<>"'()]+)""")
_SHORTENERS = frozenset(
    {
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "is.gd",
        "buff.ly",
        "rebrand.ly",
        "cutt.ly",
        "shorturl.at",
        "tiny.cc",
        "rb.gy",
    }
)
_ATTACHMENT_RE = re.compile(
    r"\b(attached|attachment|attachments|attaching|enclosed|herewith|PFA|"
    r"find the (?:file|document)|see the (?:attached|file|document))\b",
    re.IGNORECASE,
)
_OPT_OUT_RE = re.compile(
    r"(unsubscribe|opt[\s-]?out|reply\s+[\"'“]?stop|won'?t (?:contact|email) you again|"
    r"prefer not to hear|remove you from|no longer (?:wish|want) to (?:hear|receive))",
    re.IGNORECASE,
)


def _texts(payload: dict[str, Any]) -> list[str]:
    return [str(payload[k]) for k in _TEXT_FIELDS if isinstance(payload.get(k), str)]


def _addresses(payload: dict[str, Any]) -> list[str]:
    raw: list[str] = []
    for field in _RECIPIENT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            raw.append(value)
        elif isinstance(value, list):
            raw.extend(str(v) for v in value)
    return [address.strip().lower() for _, address in getaddresses(raw) if address.strip()]


async def secrets_scan(ctx: ValidationContext) -> ValidationResult:
    findings = scan_payload(ctx.payload)
    blocking = [f for f in findings if f.severity == "block"]
    if blocking:
        kinds = ", ".join(sorted({f"{f.kind} at {f.path}" for f in blocking}))
        return ValidationResult(
            "secrets_scan", "block", f"Contains a secret or card number: {kinds}"
        )
    if findings:
        kinds = ", ".join(sorted({f.kind for f in findings}))
        return ValidationResult("secrets_scan", "warn", f"Contains sensitive identifiers: {kinds}")
    return ValidationResult("secrets_scan", "pass", "No secrets found")


def _link_problems(urls: Iterable[str]) -> tuple[list[str], list[str]]:
    blocks: list[str] = []
    warns: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if scheme in {"javascript", "data", "vbscript", "file"}:
            blocks.append(f"dangerous link scheme '{scheme}:'")
            continue
        if scheme in {"http", "ftp"}:
            warns.append(f"insecure link ({scheme}://{host})")
        try:
            ipaddress.ip_address(host)
            warns.append(f"link points at a raw IP address ({host})")
        except ValueError:
            pass
        if host.startswith("xn--") or ".xn--" in host:
            warns.append(f"look-alike (punycode) domain {host}")
        if host in _SHORTENERS:
            warns.append(f"shortened link hides its destination ({host})")
    return blocks, warns


async def links_safe(ctx: ValidationContext) -> ValidationResult:
    urls = [m.group(1) for _, text in iter_strings(ctx.payload) for m in _URL_RE.finditer(text)]
    blocks, warns = _link_problems(urls)
    if blocks:
        return ValidationResult("links_safe", "block", "; ".join(sorted(set(blocks))))
    if warns:
        return ValidationResult("links_safe", "warn", "; ".join(sorted(set(warns))))
    return ValidationResult("links_safe", "pass", f"{len(urls)} link(s) checked")


async def recipients_known(ctx: ValidationContext) -> ValidationResult:
    addresses = _addresses(ctx.payload)
    if not addresses:
        return ValidationResult("recipients_known", "block", "No recipients")
    invalid = [a for a in addresses if not _EMAIL_RE.match(a)]
    if invalid:
        return ValidationResult(
            "recipients_known", "block", f"Invalid address(es): {', '.join(invalid)}"
        )
    unknown = [a for a in addresses if not await ctx.contacts.is_known(ctx.session, a)]
    notes: list[str] = []
    if unknown:
        notes.append(f"New recipient(s), check carefully: {', '.join(sorted(set(unknown)))}")
    if ctx.payload.get("bcc"):
        notes.append("Uses BCC")
    if notes:
        return ValidationResult("recipients_known", "warn", "; ".join(notes))
    return ValidationResult("recipients_known", "pass", "All recipients are known contacts")


async def attachment_mentioned(ctx: ValidationContext) -> ValidationResult:
    mentions = any(_ATTACHMENT_RE.search(t) for t in _texts(ctx.payload))
    has_attachments = bool(ctx.payload.get("attachments"))
    if mentions and not has_attachments:
        return ValidationResult(
            "attachment_mentioned",
            "warn",
            "The message mentions an attachment but none is attached",
        )
    return ValidationResult("attachment_mentioned", "pass", "Attachments consistent")


async def opt_out_present(ctx: ValidationContext) -> ValidationResult:
    if any(_OPT_OUT_RE.search(t) for t in _texts(ctx.payload)):
        return ValidationResult("opt_out_present", "pass", "Opt-out line present")
    return ValidationResult(
        "opt_out_present",
        "block",
        "Outreach must tell the recipient how to opt out (Kenya DPA s.37)",
    )


VALIDATORS: dict[str, ValidatorFn] = {
    "secrets_scan": secrets_scan,
    "links_safe": links_safe,
    "recipients_known": recipients_known,
    "attachment_mentioned": attachment_mentioned,
    "opt_out_present": opt_out_present,
}
