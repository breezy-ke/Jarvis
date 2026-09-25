"""Reading Gmail messages, and writing the replies Jarvis sends.

Reading follows what *you* would see: when an email has an HTML part (what
Gmail shows you), its visible text is what Jarvis reads, not a plain-text
alternative you never see. Text hidden with CSS is dropped and noted, because
hiding instructions from the reader is a classic way to talk to an AI behind
your back. Links keep their real destination next to their text.

Writing is deterministic: the same approved payload always gives the same
bytes (Gmail adds the Date and Message-ID itself).
"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any

from jarvis.security.untrusted import injection_signals

log = logging.getLogger("jarvis.mail")

MAX_BODY_CHARS = 100_000
_ACTION_HEADER = "X-Jarvis-Action"
_RE_PREFIX = re.compile(r"^\s*((re|aw|sv|fw|fwd|tr)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")
_AUTH_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.IGNORECASE)

# --- Reading ----------------------------------------------------------------------


@dataclass
class ParsedMessage:
    id: str
    thread_id: str
    history_id: int
    internal_date: datetime
    label_ids: list[str]
    from_name: str
    from_address: str
    to: list[tuple[str, str]]
    cc: list[tuple[str, str]]
    reply_to: list[tuple[str, str]]
    subject: str
    snippet: str
    body: str
    message_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    list_unsubscribe: bool = False
    precedence: str | None = None
    auth: dict[str, str] = field(default_factory=dict)
    attachments: list[str] = field(default_factory=list)
    hidden_text: bool = False  # the HTML hid text from the reader
    plain_text: str = ""  # the plain-text alternative, if any (checked, never shown)
    # AI-directed phrasing in text you can't see: CSS-hidden HTML, or a plain-text
    # part that says something different from what Gmail shows you.
    concealed_signals: tuple[str, ...] = ()
    jarvis_action: str | None = None
    size: int = 0

    @property
    def names(self) -> dict[str, str]:
        names = {self.from_address: self.from_name}
        for name, address in (*self.to, *self.cc, *self.reply_to):
            if name and address not in names:
                names[address] = name
        return {a: n for a, n in names.items() if a and n}


def decode_value(value: str) -> str:
    """Decode RFC 2047 encoded-words (=?utf-8?...?=), if any."""
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError, UnicodeDecodeError):
        return value


def _clean_header(value: str) -> str:
    return " ".join(decode_value(value).replace("\r", " ").replace("\n", " ").split())


def _addresses(value: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name, address in getaddresses([value]):
        address = address.strip().lower()
        if "@" in address:
            out.append((_clean_header(name).strip("\"' "), address))
    return out


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _part_text(part: dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    raw = _b64(data)
    charset = "utf-8"
    for header in part.get("headers") or []:
        if str(header.get("name", "")).lower() == "content-type":
            match = re.search(r'charset="?([\w.-]+)', str(header.get("value", "")), re.IGNORECASE)
            if match:
                charset = match.group(1)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any], texts: dict[str, list[str]], attachments: list[str]) -> None:
    mime = str(part.get("mimeType", "")).lower()
    filename = str(part.get("filename") or "")
    if filename:
        attachments.append(_clean_header(filename)[:200])
        return
    if mime in ("text/plain", "text/html"):
        texts[mime].append(_part_text(part))
    for child in part.get("parts") or []:
        _walk(child, texts, attachments)


def parse_gmail_message(resource: dict[str, Any]) -> ParsedMessage:
    """A Gmail API message (format=full) as plain, checked fields."""
    payload = resource.get("payload") or {}
    headers: dict[str, list[str]] = {}
    for header in payload.get("headers") or []:
        headers.setdefault(str(header.get("name", "")).lower(), []).append(
            str(header.get("value", ""))
        )

    def first(name: str) -> str:
        values = headers.get(name.lower())
        return values[0] if values else ""

    texts: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    attachments: list[str] = []
    _walk(payload, texts, attachments)
    html = "\n".join(t for t in texts["text/html"] if t)
    plain = "\n".join(t for t in texts["text/plain"] if t)
    hidden = False
    concealed = ""
    if html:
        visible = html_to_text(html)
        body, hidden, concealed = visible.text, visible.hidden, visible.hidden_text
        if plain and plain_differs(plain, body):
            concealed += "\n" + plain
    else:
        body = plain
    from_name, from_address = parseaddr(_clean_header(first("from")))
    auth: dict[str, str] = {}
    for line in headers.get("authentication-results", [])[:1]:  # the receiving server's own
        for key, result in _AUTH_RE.findall(line):
            auth.setdefault(key.lower(), result.lower())
    internal_ms = int(resource.get("internalDate") or 0)
    return ParsedMessage(
        id=str(resource["id"]),
        thread_id=str(resource.get("threadId") or resource["id"]),
        history_id=int(resource.get("historyId") or 0),
        internal_date=datetime.fromtimestamp(internal_ms / 1000, tz=UTC),
        label_ids=[str(label) for label in resource.get("labelIds") or []],
        from_name=_clean_header(from_name).strip("\"' "),
        from_address=from_address.strip().lower(),
        to=_addresses(first("to")),
        cc=_addresses(first("cc")),
        reply_to=_addresses(first("reply-to")),
        subject=_clean_header(first("subject"))[:500],
        snippet=_clean_header(str(resource.get("snippet") or ""))[:500],
        body=_tidy(body)[:MAX_BODY_CHARS],
        message_id=first("message-id").strip() or None,
        in_reply_to=first("in-reply-to").strip() or None,
        references=" ".join(first("references").split()) or None,
        list_unsubscribe=bool(first("list-unsubscribe")),
        precedence=first("precedence").strip().lower() or None,
        auth=auth,
        attachments=attachments,
        hidden_text=hidden,
        plain_text=_tidy(plain)[:MAX_BODY_CHARS] if html else "",
        concealed_signals=injection_signals(concealed[:20_000]) if concealed.strip() else (),
        jarvis_action=first(_ACTION_HEADER).strip() or None,
        size=int(resource.get("sizeEstimate") or 0),
    )


def plain_differs(plain: str, visible: str) -> bool:
    """True when a plain-text alternative says a lot the visible HTML doesn't."""
    visible_words = set(visible.lower().split())
    extra = [w for w in plain.lower().split() if w not in visible_words]
    return len(extra) > max(15, len(plain.split()) // 4)


def _tidy(text: str) -> str:
    lines = [_WS_RE.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    return _BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()


def thread_subject(subject: str) -> str:
    """The subject without "Re:", "Fwd:" and friends."""
    return _RE_PREFIX.sub("", subject).strip() or "(no subject)"


def reply_subject(subject: str, *, limit: int = 300) -> str:
    base = " ".join(thread_subject(subject).split())
    reply = base if base == "(no subject)" else f"Re: {base}"
    return reply if len(reply) <= limit else reply[: limit - 1].rstrip() + "…"


# --- HTML to what a person sees ---------------------------------------------------

_SKIP = frozenset({"script", "style", "head", "title", "noscript", "template", "svg", "object"})
_VOID = frozenset(
    {"br", "img", "hr", "meta", "link", "input", "area", "base", "col", "wbr", "source", "embed"}
)
_BLOCK = frozenset(
    {
        "p",
        "div",
        "li",
        "tr",
        "table",
        "blockquote",
        "section",
        "article",
        "header",
        "footer",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "center",
    }
)
_HIDING_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d]*[1-9])|"
    r"opacity\s*:\s*0(?![.\d]*[1-9])|max-height\s*:\s*0(?![.\d]*[1-9])|"
    r"(?<![-\w])(width|height)\s*:\s*0(?![.\d]*[1-9])",
    re.IGNORECASE,
)


@dataclass
class VisibleText:
    text: str
    hidden: bool  # some text was hidden from the reader (and dropped here)
    hidden_text: str = ""  # what was hidden, for checks only


class _Visible(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hidden_out: list[str] = []
        self.hidden_chars = 0
        self._stack: list[tuple[str, bool]] = []
        self._links: list[str | None] = []

    @property
    def _hidden(self) -> bool:
        return bool(self._stack) and self._stack[-1][1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        hidden = (
            self._hidden
            or tag in _SKIP
            or "hidden" in values
            or (values.get("aria-hidden") == "true" and tag != "img")
            or bool(_HIDING_STYLE.search(values.get("style", "")))
        )
        if tag in _BLOCK or tag == "br":
            self.out.append("\n")
        if tag == "img" and not hidden:
            alt = values.get("alt", "").strip()
            self.out.append(f"[image: {alt}]" if alt else "[image]")
        if tag in _VOID:
            return
        self._stack.append((tag, hidden))
        if tag == "a":
            href = values.get("href", "").strip()
            self._links.append(href if href.lower().startswith(("http", "mailto:")) else None)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        # Pop up to the matching tag; tolerate unclosed and stray tags.
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                was_hidden = self._stack[index][1]
                del self._stack[index:]
                if tag == "a" and self._links:
                    href = self._links.pop()
                    if href and not was_hidden:
                        self.out.append(f" <{href}>")
                break
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._hidden:
            if self._stack[-1][0] not in _SKIP and not any(t in _SKIP for t, _ in self._stack):
                self.hidden_chars += len(data.strip())
                self.hidden_out.append(data)
            return
        self.out.append(data)


def html_to_text(html: str) -> VisibleText:
    parser = _Visible()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep what was read so far
        log.debug("html_to_text: markup the parser couldn't finish", exc_info=True)
    return VisibleText(
        _tidy("".join(parser.out)),
        hidden=parser.hidden_chars > 20,
        hidden_text=_tidy(" ".join(parser.hidden_out))[:20_000],
    )


# --- Writing ---------------------------------------------------------------------


def build_message(
    *,
    sender: str,
    sender_name: str | None = None,
    to: Sequence[str],
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    action_id: str | None = None,
) -> bytes:
    """The exact bytes of an email Jarvis sends. Plain text, UTF-8."""
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if action_id:
        msg[_ACTION_HEADER] = action_id
    text = body.replace("\r\n", "\n").rstrip() + "\n"
    msg.set_content(text, subtype="plain", charset="utf-8", cte="quoted-printable")
    return msg.as_bytes()


def to_raw(message: bytes) -> str:
    """Gmail's `raw` field: the message, base64url-encoded."""
    return base64.urlsafe_b64encode(message).decode("ascii")
