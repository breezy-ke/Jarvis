"""Detect secrets and sensitive numbers in text an action is about to send."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str  # "block" | "warn"
    path: str
    excerpt: str


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    ),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_secret", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b")),
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{40,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "password_assignment",
        re.compile(r"\b(?:password|passwd|pwd|passcode)\s*[:=]\s*\S{6,}", re.IGNORECASE),
    ),
)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_KRA_PIN_RE = re.compile(r"\b[AP]\d{9}[A-Z]\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _mask(text: str) -> str:
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-2:]}"


def scan_text(text: str, *, path: str = "$") -> list[Finding]:
    findings: list[Finding] = []
    for kind, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(Finding(kind, "block", path, _mask(match.group(0))))
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            findings.append(Finding("payment_card_number", "block", path, _mask(digits)))
    for match in _KRA_PIN_RE.finditer(text):
        findings.append(Finding("kra_pin", "warn", path, _mask(match.group(0))))
    return findings


def redact_text(text: str, *, placeholder: str = "[hidden]") -> tuple[str, int]:
    """Replace secrets, card numbers and KRA PINs in `text`. Returns (text, how many)."""
    count = 0

    def hide(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return placeholder

    def hide_card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return hide(match)
        return match.group(0)

    for _, pattern in _SECRET_PATTERNS:
        text = pattern.sub(hide, text)
    text = _CARD_RE.sub(hide_card, text)
    text = _KRA_PIN_RE.sub(hide, text)
    return text, count


def iter_strings(value: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """Yield (path, text) for every string inside a JSON-like value."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from iter_strings(item, f"{path}[{index}]")


def scan_payload(payload: Any) -> list[Finding]:
    findings: list[Finding] = []
    for path, text in iter_strings(payload):
        findings.extend(scan_text(text, path=path))
    return findings
