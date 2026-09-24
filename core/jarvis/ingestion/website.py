"""Read the owner's portfolio or business website (politely).

Politeness rules: respect robots.txt, identify honestly in the user agent,
fetch at most `max_pages` pages from the same host, one at a time, and only
over http(s). Page text is untrusted: it is sanitised and wrapped before any
model sees it, and the model can only return profile *suggestions*.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx

USER_AGENT = "JarvisPersonalAssistant/0.1 (+owner-initiated profile import)"
_INTERESTING = (
    "about",
    "service",
    "work",
    "portfolio",
    "project",
    "case",
    "team",
    "contact",
    "pricing",
)
_MAX_BYTES = 2_000_000


class WebsiteError(ValueError):
    pass


@dataclass
class CrawlResult:
    start_url: str
    pages: list[tuple[str, str]] = field(default_factory=list)  # (url, text)
    skipped: list[str] = field(default_factory=list)


def _normalize(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebsiteError("Please give a full http(s) address, like https://example.com")
    return parsed._replace(fragment="").geturl()


def _html_to_text(html: str) -> str:
    import trafilatura

    return trafilatura.extract(html, include_links=False, include_comments=False) or ""


def _links(html: str, base: str) -> list[str]:
    import re

    host = urlparse(base).netloc
    found: list[str] = []
    for href in re.findall(r"""href=["']([^"'#]+)["']""", html, flags=re.IGNORECASE):
        url = urljoin(base, href)
        parsed = urlparse(url)
        if parsed.netloc != host or parsed.scheme not in {"http", "https"}:
            continue
        if any(word in parsed.path.lower() for word in _INTERESTING) and url not in found:
            found.append(url)
    return found


async def _robots(client: httpx.AsyncClient, url: str) -> robotparser.RobotFileParser:
    parsed = urlparse(url)
    rp = robotparser.RobotFileParser()
    try:
        resp = await client.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
    except httpx.HTTPError:
        rp.parse([])
    return rp


async def crawl(client: httpx.AsyncClient, url: str, *, max_pages: int = 6) -> CrawlResult:
    start = _normalize(url)
    result = CrawlResult(start_url=start)
    robots = await _robots(client, start)
    queue = [start]
    seen: set[str] = set()
    while queue and len(result.pages) < max_pages:
        target = queue.pop(0)
        if target in seen:
            continue
        seen.add(target)
        if not robots.can_fetch(USER_AGENT, target):
            result.skipped.append(f"{target} (disallowed by robots.txt)")
            continue
        try:
            resp = await client.get(
                target, headers={"User-Agent": USER_AGENT}, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            result.skipped.append(f"{target} ({type(exc).__name__})")
            continue
        content_type = resp.headers.get("content-type", "")
        if resp.status_code != 200 or "html" not in content_type or len(resp.content) > _MAX_BYTES:
            result.skipped.append(f"{target} (status {resp.status_code})")
            continue
        html = resp.text
        text = await asyncio.to_thread(_html_to_text, html)
        if text.strip():
            result.pages.append((target, text[:8_000]))
        if target == start:
            queue.extend(link for link in _links(html, start) if link not in seen)
        await asyncio.sleep(0)  # yield between requests; requests run sequentially anyway
    if not result.pages:
        raise WebsiteError(
            "Couldn't read any pages from that site (check the address or robots.txt)."
        )
    return result
