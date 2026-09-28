"""RSS 2.0, RSS 1.0 (RDF) and Atom feeds, parsed without trusting them.

`defusedxml` refuses entity expansion ("billion laughs") and external
entities, so a hostile feed can't exhaust memory or read local files. Only
entries with an http(s) link and a title are kept, and HTML in summaries is
reduced to the text a reader would see (nothing hidden, no scripts).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from jarvis.brief.links import clean_url
from jarvis.mail.mime import html_to_text

MAX_ENTRIES = 100
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class FeedError(ValueError):
    pass


@dataclass(frozen=True)
class FeedEntry:
    title: str
    url: str
    summary: str
    published: datetime | None


def _local(tag: object) -> str:
    name = str(tag)
    return name.rsplit("}", 1)[-1] if "}" in name else name


def _children(element: Element | None, name: str) -> list[Element]:
    if element is None:
        return []
    return [child for child in element if _local(child.tag) == name]


def _child(element: Element | None, name: str) -> Element | None:
    found = _children(element, name)
    return found[0] if found else None


def _text(element: Element | None, name: str) -> str:
    child = _child(element, name)
    return "".join(child.itertext()).strip() if child is not None else ""


def clean_title(text: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", text))).strip()[:300]


def clean_summary(text: str, *, limit: int = 1_000) -> str:
    if "<" in text:
        text = html_to_text(text).text
    text = _SPACE.sub(" ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(value)  # RSS: "Mon, 28 Sep 2026 06:00:00 GMT"
    except (TypeError, ValueError, IndexError):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom, dc:date
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _http(url: str, base: str) -> str | None:
    absolute = urljoin(base, url.strip())
    return clean_url(absolute) if absolute.startswith(("https://", "http://")) else None


def _rss_item(item: Element, base: str) -> FeedEntry | None:
    link = _text(item, "link")
    if not link:
        guid = _child(item, "guid")
        if guid is not None and guid.get("isPermaLink", "true") != "false":
            link = "".join(guid.itertext())
    url = _http(link, base) if link else None
    title = clean_title(_text(item, "title"))
    if not url or not title:
        return None
    body = _text(item, "encoded") or _text(item, "description")
    published = parse_date(_text(item, "pubDate") or _text(item, "date"))
    return FeedEntry(title=title, url=url, summary=clean_summary(body), published=published)


def _atom_entry(entry: Element, base: str) -> FeedEntry | None:
    href = ""
    for link in _children(entry, "link"):
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            href = link.get("href", "")
            break
    if not href:
        first = _child(entry, "link")
        href = first.get("href", "") if first is not None else ""
    url = _http(href, base) if href else None
    title = clean_title(_text(entry, "title"))
    if not url or not title:
        return None
    body = _text(entry, "summary") or _text(entry, "content")
    published = parse_date(_text(entry, "published") or _text(entry, "updated"))
    return FeedEntry(title=title, url=url, summary=clean_summary(body), published=published)


def parse_feed(data: bytes, *, base_url: str) -> list[FeedEntry]:
    """The feed's entries, newest first as published, at most `MAX_ENTRIES`."""
    try:
        root = fromstring(data, forbid_dtd=False, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise FeedError(f"not a readable feed ({type(exc).__name__})") from exc
    kind = _local(root.tag).lower()
    if kind == "rss":
        raw = [_rss_item(i, base_url) for i in _children(_child(root, "channel"), "item")]
    elif kind == "rdf":
        raw = [_rss_item(i, base_url) for i in _children(root, "item")]
    elif kind == "feed":
        raw = [_atom_entry(e, base_url) for e in _children(root, "entry")]
    else:
        raise FeedError(f"not an RSS or Atom feed (<{kind}>)")
    return [entry for entry in raw if entry is not None][:MAX_ENTRIES]
