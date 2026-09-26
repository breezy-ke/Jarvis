"""Handling of untrusted content (emails, web pages, documents, search results).

Three layers of prompt-injection defence live here:

1. **Sanitise.** Strip content that is invisible to a human reader but visible
   to a model: zero-width and bidi-control characters, Unicode "tag"
   characters, variation selectors, and HTML comments.
2. **Wrap.** Enclose the content in markers carrying a random nonce, so the
   content cannot close the wrapper early. The persona tells every agent to
   treat wrapped content as data, never as instructions.
3. **Flag.** Detect common injection phrasing, so the owner can be warned.

These layers are necessary but not sufficient. The real guarantee is
architectural: agents that read untrusted content have no side-effect tools,
and every outward action needs policy approval.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass, field

_INVISIBLE_RE = re.compile(
    "["
    "­"  # soft hyphen
    "᠎"  # Mongolian vowel separator
    "​-‏"  # zero-width space/joiners, LRM/RLM
    "‪-‮"  # bidi embeddings/overrides
    "⁠-⁤"  # word joiner, invisible operators
    "⁦-⁩"  # bidi isolates
    "︀-️"  # variation selectors
    "﻿"  # BOM / zero-width no-break space
    "\U000e0000-\U000e007f"  # Unicode tag characters ("ASCII smuggling")
    "\U000e0100-\U000e01ef"  # variation selectors supplement
    "]"
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_WRAPPER_TAG_RE = re.compile(r"</?\s*untrusted", re.IGNORECASE)
_WRAPPED_RE = re.compile(r'<untrusted nonce="[0-9a-f]{8}"')
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your)\b"
            r".{0,30}\b(instructions?|prompts?|rules|directions)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "role_hijack",
        re.compile(
            r"\b(you are now|act as|pretend to be|new instructions|system prompt|developer mode)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "fake_role_marker",
        re.compile(
            r"^\s*(system|assistant|developer)\s*:|\[/?(system|inst)\]|<\|im_start\|>",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "exfiltration_request",
        re.compile(
            r"\b(forward|send|email|share|upload|post)\b.{0,60}"
            r"\b(password|credentials?|api[ _-]?keys?|tokens?|inbox|"
            r"all (?:emails|messages|files)|contacts)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "secrecy_request",
        re.compile(
            r"\b(do not|don't|never)\b.{0,20}\b(tell|inform|notify|mention|alert)\b.{0,20}"
            r"\b(the )?(user|owner|him|her|them)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "tool_invocation",
        re.compile(
            r"\b(call|invoke|use|run)\b.{0,20}\b(tool|function|propose_action)\b",
            re.IGNORECASE,
        ),
    ),
    # Swahili: "puuza maagizo yote ya awali" (ignore all previous instructions)
    (
        "override_instructions_sw",
        re.compile(
            r"\b(puuza|sahau|dharau)\b.{0,40}\b(maagizo|maelekezo|amri)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # Swahili: "tuma barua pepe zote / nywila kwa ..." (send all emails / the password to ...)
    (
        "exfiltration_request_sw",
        re.compile(
            r"\b(tuma|peleka|sambaza)\b.{0,60}"
            r"\b(nywila|nenosiri|neno la siri|barua pepe zote|jumbe zote|anwani zote)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)


@dataclass(frozen=True)
class SanitizedText:
    text: str
    removed_invisible: int = 0
    removed_comments: int = 0
    truncated: bool = False
    signals: tuple[str, ...] = field(default_factory=tuple)

    @property
    def suspicious(self) -> bool:
        return bool(self.signals) or self.removed_invisible > 0 or self.removed_comments > 0


def injection_signals(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in _INJECTION_PATTERNS if pattern.search(text))


def sanitize(text: str, *, max_chars: int = 20_000) -> SanitizedText:
    """Remove hidden content and normalise untrusted text before a model sees it."""
    normalized = unicodedata.normalize("NFKC", text)
    without_comments, removed_comments = _HTML_COMMENT_RE.subn("", normalized)
    cleaned, removed_invisible = _INVISIBLE_RE.subn("", without_comments)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned).strip()
    truncated = len(cleaned) > max_chars
    if truncated:
        cleaned = cleaned[:max_chars].rstrip() + "\n[... truncated ...]"
    return SanitizedText(
        text=cleaned,
        removed_invisible=removed_invisible,
        removed_comments=removed_comments,
        truncated=truncated,
        signals=injection_signals(cleaned),
    )


def _attr(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _.:/@#?=&%+-]", "", value)[:200]


def contains_untrusted(text: str) -> bool:
    """Whether `text` carries content wrapped by `wrap` (someone else's words)."""
    return _WRAPPED_RE.search(text) is not None


def wrap(text: str, *, source: str, kind: str, max_chars: int = 20_000) -> str:
    """Sanitise `text` and enclose it in nonce-tagged untrusted-content markers."""
    sanitized = sanitize(text, max_chars=max_chars)
    body = _WRAPPER_TAG_RE.sub(lambda m: m.group(0).replace("<", "&lt;"), sanitized.text)
    nonce = secrets.token_hex(4)
    warning = ""
    if sanitized.suspicious:
        reasons = list(sanitized.signals)
        if sanitized.removed_invisible:
            reasons.append(f"removed {sanitized.removed_invisible} hidden characters")
        if sanitized.removed_comments:
            reasons.append(f"removed {sanitized.removed_comments} hidden HTML comments")
        warning = f' warning="possible prompt injection: {_attr("; ".join(reasons))}"'
    return (
        f'<untrusted nonce="{nonce}" kind="{_attr(kind)}" source="{_attr(source)}"{warning}>\n'
        f"{body}\n"
        f'</untrusted nonce="{nonce}">'
    )
