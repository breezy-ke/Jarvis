"""The brief as you see it: in the app, on Telegram, in your inbox, and read aloud.

Every version is drawn from the stored entries, so:

* every item links to its source's own address, and only an http(s) one;
* everything else is escaped text, so a headline can't add markup or links;
* "Why this?" is the ranking's own reasons, in plain words.
"""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from jarvis.brief.build import watch_lines
from jarvis.brief.rank import explain
from jarvis.db.models import Brief, BriefEntry
from jarvis.security.scanners import redact_text

SECTIONS = ("top", "security", "africa", "quick")
HEADINGS = {
    "top": "Top stories",
    "security": "Security watch",
    "africa": "Kenya and Africa",
    "quick": "Quick hits",
}
QUIET = "Nothing urgent today."
TELEGRAM_CHARS = 4_000  # visible characters; Telegram takes 4,096 a message
MASKED = "[hidden: see the app]"
_VOTE = re.compile(r"bv:([0-9a-f-]{36}):([ud])")
_TAG = re.compile(r"<[^>]*>")


def long_day(day: date) -> str:
    return f"{day:%A} {day.day} {day:%B}"


def safe_url(url: str) -> str | None:
    """The link, if it's a plain http(s) address; anything else isn't shown as a link."""
    if not url or len(url) > 4_000 or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.hostname else None


@dataclass(frozen=True)
class EntryView:
    id: uuid.UUID
    section: str
    rank: int
    title: str
    url: str
    source: str
    summary: str
    why: str = ""
    client: str = ""
    reasons: tuple[str, ...] = ()
    watch: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    vote: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "section": self.section,
            "rank": self.rank,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "summary": self.summary,
            "why": self.why,
            "client": self.client,
            "reasons": list(self.reasons),
            "watch": list(self.watch),
            "details": dict(self.details),
            "vote": self.vote,
        }


@dataclass(frozen=True)
class BriefView:
    id: uuid.UUID
    day: date
    status: str
    scheduled_for: datetime | None
    delivered_at: datetime | None
    on_time: bool | None
    do_today: str
    script: str
    entries: tuple[EntryView, ...]
    audio_seconds: float | None = None
    audio_file: str | None = None
    deliveries: Mapping[str, Any] = field(default_factory=dict)
    models: Mapping[str, Any] = field(default_factory=dict)

    def section(self, name: str) -> list[EntryView]:
        return [e for e in self.entries if e.section == name]

    @property
    def headline(self) -> str | None:
        top = self.section("top")
        return top[0].title if top else None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "day": self.day.isoformat(),
            "status": self.status,
            "scheduled_for": self.scheduled_for.isoformat() if self.scheduled_for else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "on_time": self.on_time,
            "do_today": self.do_today,
            "sections": {name: [e.to_json() for e in self.section(name)] for name in SECTIONS},
            "audio_seconds": self.audio_seconds,
            "audio_url": f"/api/brief/audio/{self.audio_file}" if self.audio_file else None,
            "deliveries": dict(self.deliveries),
            "models": dict(self.models),
        }


def build_view(
    brief: Brief,
    entries: list[BriefEntry],
    *,
    decrypt: Callable[[bytes | None, Any], Any],
    source_names: Mapping[str, str],
) -> BriefView:
    """A stored brief, decrypted and in section order, ready to show."""
    extra = decrypt(brief.extra_enc, {})
    order = {name: n for n, name in enumerate(SECTIONS)}
    views: list[EntryView] = []
    for entry in sorted(entries, key=lambda e: (order.get(e.section, 9), e.rank)):
        note = decrypt(entry.note_enc, {})
        details = dict(entry.details or {})
        views.append(
            EntryView(
                id=entry.id,
                section=entry.section,
                rank=entry.rank,
                title=entry.title,
                url=entry.url,
                source=entry.source_name,
                summary=entry.summary or "",
                why=str(note.get("why") or ""),
                client=str(note.get("client") or ""),
                reasons=tuple(explain(dict(entry.why or {}), dict(source_names)))
                if entry.section != "security"
                else (),
                watch=tuple(watch_lines(details)) if entry.section == "security" else (),
                details=details,
                vote=entry.vote,
            )
        )
    return BriefView(
        id=brief.id,
        day=brief.day,
        status=brief.status,
        scheduled_for=brief.scheduled_for,
        delivered_at=brief.delivered_at,
        on_time=brief.on_time,
        do_today=str(extra.get("do_today") or ""),
        script=str(extra.get("script") or ""),
        entries=tuple(views),
        audio_seconds=brief.audio_seconds,
        audio_file=brief.audio_file,
        deliveries=dict(brief.deliveries or {}),
        models=dict(brief.models or {}),
    )


# --- Push -----------------------------------------------------------------------------------


def push_text(view: BriefView) -> tuple[str, str]:
    """The notification: a title and one line."""
    if view.headline:
        return "Your tech brief", view.headline[:180]
    if view.section("security"):
        return "Your tech brief", view.section("security")[0].title[:180]
    return "Your tech brief", "A quiet morning: nothing new worth your time."


# --- Telegram -------------------------------------------------------------------------------

Buttons = list[list[dict[str, str]]]


def vote_data(entry_id: uuid.UUID, vote: int) -> str:
    return f"bv:{entry_id}:{'u' if vote > 0 else 'd'}"


def parse_vote(data: str) -> tuple[uuid.UUID, int] | None:
    """The entry and the vote in a 👍/👎 button's data, or None if it isn't one."""
    match = _VOTE.fullmatch(data)
    if match is None:
        return None
    try:
        entry_id = uuid.UUID(match.group(1))
    except ValueError:
        return None
    return entry_id, 1 if match.group(2) == "u" else -1


def _t(text: str, limit: int = 500) -> str:
    text = text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return html.escape(text, quote=False)


def _said(text: str, limit: int = 300) -> str:
    """A model's words on Telegram: anything secret-looking is masked, then escaped."""
    clean, _ = redact_text(text, placeholder=MASKED)
    return _t(clean, limit)


def _link(entry: EntryView, limit: int = 200) -> str:
    url = safe_url(entry.url)
    title = _t(entry.title, limit)
    return f'<a href="{html.escape(url, quote=True)}">{title}</a>' if url else title


def _vote_row(entry: EntryView) -> list[dict[str, str]]:
    up = "👍" + (" ✓" if entry.vote == 1 else "")
    down = "👎" + (" ✓" if entry.vote == -1 else "")
    return [
        {"text": f"{entry.rank} {up}", "callback_data": vote_data(entry.id, 1)},
        {"text": f"{entry.rank} {down}", "callback_data": vote_data(entry.id, -1)},
    ]


def telegram_buttons(entries: list[EntryView]) -> Buttons:
    return [_vote_row(e) for e in entries]


def telegram_units(markup: str) -> int:
    """What Telegram counts against its limit: the visible text, in UTF-16 units."""
    visible = html.unescape(_TAG.sub("", markup))
    return len(visible.encode("utf-16-le")) // 2


def telegram_messages(view: BriefView, *, app_link: str | None = None) -> list[tuple[str, Buttons]]:
    """The brief as Telegram messages (HTML), each with the 👍/👎 buttons for its stories."""
    blocks: list[tuple[str, EntryView | None]] = [
        (f"☕ <b>Your tech brief</b> · {long_day(view.day)}", None)
    ]
    top = view.section("top")
    if top:
        blocks.append((f"<b>{HEADINGS['top']}</b>", None))
    for entry in top:
        lines = [f"<b>{entry.rank}.</b> {_link(entry)}", f"<i>{_t(entry.source, 80)}</i>"]
        if entry.summary:
            lines.append(_t(entry.summary, 450))
        if entry.why:
            lines.append(f"💡 {_said(entry.why)}")
        if entry.client:
            lines.append(f"🤝 {_said(entry.client)}")
        blocks.append(("\n".join(lines), entry))
    watch = view.section("security")
    blocks.append((f"🛡 <b>{HEADINGS['security']}</b>" + ("" if watch else f"\n{QUIET}"), None))
    for entry in watch:
        lines = [f"• {_link(entry)}", *(f"   {_t(line, 300)}" for line in entry.watch)]
        blocks.append(("\n".join(lines), None))
    for name, icon in (("africa", "🌍"), ("quick", "⚡")):
        chosen = view.section(name)
        if chosen:
            blocks.append((f"{icon} <b>{HEADINGS[name]}</b>", None))
        for entry in chosen:
            line = f"• {_link(entry)} · <i>{_t(entry.source, 80)}</i>"
            if name == "africa" and entry.summary:
                line += f"\n   {_t(entry.summary, 300)}"
            blocks.append((line, None))
    if view.do_today:
        blocks.append((f"✅ <b>One thing to do today</b>\n{_said(view.do_today, 400)}", None))
    if top:
        footer = "Tap 👍 or 👎 under a story, and tomorrow's brief fits you better."
        if app_link and safe_url(app_link):
            footer += f' <a href="{html.escape(app_link, quote=True)}">Open in Jarvis</a>'
        blocks.append((footer, None))

    messages: list[tuple[str, Buttons]] = []
    text, voted = "", []
    for block, entry in blocks:
        if text and telegram_units(text) + 2 + telegram_units(block) > TELEGRAM_CHARS:
            messages.append((text, telegram_buttons(voted)))
            text, voted = "", []
        text = f"{text}\n\n{block}" if text else block
        if entry is not None:
            voted.append(entry)
    if text:
        messages.append((text, telegram_buttons(voted)))
    return messages


# --- Email -----------------------------------------------------------------------------------


def email_subject(view: BriefView) -> str:
    return f"Your tech brief, {long_day(view.day)}"


def email_text(view: BriefView, *, app_link: str | None = None) -> str:
    """The plain-text part: every story with its link written out."""
    out = [f"Your tech brief · {long_day(view.day)}", ""]
    top = view.section("top")
    if top:
        out += [HEADINGS["top"].upper(), ""]
    for entry in top:
        out.append(f"{entry.rank}. {entry.title}")
        out.append(f"   {entry.source} · {safe_url(entry.url) or ''}".rstrip(" ·"))
        if entry.summary:
            out.append(f"   {entry.summary}")
        if entry.why:
            out.append(f"   Why it matters: {entry.why}")
        if entry.client:
            out.append(f"   Client angle: {entry.client}")
        out.append("")
    out += [HEADINGS["security"].upper(), ""]
    for entry in view.section("security"):
        out.append(f"- {entry.title}")
        out += [f"  {line}" for line in entry.watch]
        out.append(f"  {safe_url(entry.url) or ''}")
    if not view.section("security"):
        out.append(QUIET)
    out.append("")
    for name in ("africa", "quick"):
        chosen = view.section(name)
        if not chosen:
            continue
        out += [HEADINGS[name].upper(), ""]
        for entry in chosen:
            out.append(f"- {entry.title} ({entry.source})")
            out.append(f"  {safe_url(entry.url) or ''}")
        out.append("")
    if view.do_today:
        out += ["ONE THING TO DO TODAY", "", view.do_today, ""]
    if app_link and safe_url(app_link):
        out += [f"Listen, and tell Jarvis what you liked: {app_link}", ""]
    out.append("Jarvis put this copy in your inbox; it wasn't sent over email.")
    return "\n".join(out).strip() + "\n"


_CSS = {
    "body": "margin:0;padding:24px 16px;background:#f6f5f2;color:#1c1b19;"
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.5",
    "card": "max-width:640px;margin:0 auto;background:#ffffff;border-radius:12px;padding:24px",
    "h1": "font-size:22px;margin:0 0 4px",
    "day": "color:#6b6860;margin:0 0 20px",
    "h2": "font-size:16px;margin:24px 0 8px;padding-top:16px;border-top:1px solid #e7e4dc",
    "item": "margin:0 0 16px",
    "title": "font-weight:600;color:#1a56db;text-decoration:none",
    "source": "color:#6b6860;font-size:13px",
    "note": "margin:4px 0 0;padding-left:10px;border-left:3px solid #e8b04b",
    "muted": "color:#6b6860;font-size:13px",
}


def _a(entry: EntryView) -> str:
    url = safe_url(entry.url)
    title = html.escape(entry.title, quote=False)
    if url is None:
        return f'<span style="{_CSS["title"]}">{title}</span>'
    return f'<a href="{html.escape(url, quote=True)}" style="{_CSS["title"]}">{title}</a>'


def email_html(view: BriefView, *, app_link: str | None = None) -> str:
    """The HTML part: inline styles only, no images, no scripts, every link a source's."""
    e = html.escape
    parts = [
        f'<div style="{_CSS["card"]}">',
        f'<h1 style="{_CSS["h1"]}">Your tech brief</h1>',
        f'<p style="{_CSS["day"]}">{e(long_day(view.day))}</p>',
    ]
    top = view.section("top")
    if top:
        parts.append(f'<h2 style="{_CSS["h2"]}">{HEADINGS["top"]}</h2>')
    for entry in top:
        parts.append(f'<div style="{_CSS["item"]}">{entry.rank}. {_a(entry)}')
        parts.append(f'<div style="{_CSS["source"]}">{e(entry.source)}</div>')
        if entry.summary:
            parts.append(f'<p style="margin:4px 0 0">{e(entry.summary)}</p>')
        if entry.why:
            parts.append(f'<p style="{_CSS["note"]}"><b>Why it matters:</b> {e(entry.why)}</p>')
        if entry.client:
            parts.append(f'<p style="{_CSS["note"]}"><b>Client angle:</b> {e(entry.client)}</p>')
        parts.append("</div>")
    parts.append(f'<h2 style="{_CSS["h2"]}">{HEADINGS["security"]}</h2>')
    for entry in view.section("security"):
        lines = "".join(f"<br>{e(line)}" for line in entry.watch)
        parts.append(f'<p style="{_CSS["item"]}">{_a(entry)}{lines}</p>')
    if not view.section("security"):
        parts.append(f"<p>{QUIET}</p>")
    for name in ("africa", "quick"):
        chosen = view.section(name)
        if not chosen:
            continue
        parts.append(
            f'<h2 style="{_CSS["h2"]}">{HEADINGS[name]}</h2><ul style="padding-left:18px">'
        )
        for entry in chosen:
            parts.append(
                f'<li style="margin:0 0 8px">{_a(entry)} '
                f'<span style="{_CSS["source"]}">· {e(entry.source)}</span></li>'
            )
        parts.append("</ul>")
    if view.do_today:
        parts.append(f'<h2 style="{_CSS["h2"]}">One thing to do today</h2>')
        parts.append(f"<p>{e(view.do_today)}</p>")
    footer = "Jarvis put this copy in your inbox; it wasn't sent over email."
    if app_link and safe_url(app_link):
        link = html.escape(app_link, quote=True)
        footer = f'<a href="{link}">Listen, and tell Jarvis what you liked</a>. ' + footer
    parts.append(f'<p style="{_CSS["muted"]};margin-top:24px">{footer}</p></div>')
    body = "\n".join(parts)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{e(email_subject(view))}</title></head>"
        f'<body style="{_CSS["body"]}">{body}</body></html>'
    )
