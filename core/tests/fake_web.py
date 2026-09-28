"""A small fake of the public web for the brief and research tests.

Sites are told apart by the Host header (the safe fetcher connects to the
checked address and names the site in Host, as a real browser would). Pages,
feeds and the Hacker News, GitHub, CISA and SearXNG APIs can be set up per
test, along with slow, huge, redirecting and hostile pages.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from jarvis.security.fetch import Resolver, SafeFetcher

PUBLIC_ADDRESS = "93.184.216.34"
Handler = Callable[[Request, dict[str, list[str]]], Response]


@dataclass
class Page:
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


def _split(url: str) -> tuple[str, str, str]:
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.path or "/", parts.query


class FakeWeb:
    def __init__(self) -> None:
        self.pages: dict[str, Page] = {}  # "host/path" or "host/path?query"
        self.handlers: dict[str, Handler] = {}  # "host/path"
        self.addresses: dict[str, list[str]] = {}  # host -> addresses (default: public)
        self.requests: list[str] = []  # "host/path?query"
        self.app = self._build_app()

    # --- Wiring --------------------------------------------------------------------------

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)

    def resolver(self) -> Resolver:
        async def resolve(host: str, port: int) -> list[str]:
            if host in self.addresses:
                return self.addresses[host]
            return [PUBLIC_ADDRESS]

        return resolve

    def fetcher(self, **options: Any) -> SafeFetcher:
        return SafeFetcher(
            transport=self.transport(),
            resolver=self.resolver(),
            per_host_interval=0.0,
            **options,
        )

    def hits(self, host: str, path: str = "") -> int:
        return sum(1 for r in self.requests if r.startswith(f"{host}{path}"))

    # --- Content --------------------------------------------------------------------------

    def page(self, url: str, body: str | bytes = b"", **options: Any) -> None:
        host, path, query = _split(url)
        key = f"{host}{path}" + (f"?{query}" if query else "")
        data = body.encode() if isinstance(body, str) else body
        self.pages[key] = Page(body=data, **options)

    def redirect(self, url: str, to: str, status: int = 301) -> None:
        self.page(url, status=status, headers={"location": to})

    def feed(
        self,
        url: str,
        entries: Iterable[dict[str, Any]],
        *,
        kind: str = "rss",
        etag: str | None = None,
    ) -> None:
        """A feed of `entries`: {title, link, summary, published (datetime)}."""
        items = list(entries)
        if kind == "atom":
            parts = [
                "<entry>"
                f"<title>{escape(e['title'])}</title>"
                f'<link rel="alternate" href="{escape(e["link"])}"/>'
                f"<summary>{escape(e.get('summary', ''))}</summary>"
                f"<updated>{e['published'].isoformat()}</updated>"
                "</entry>"
                for e in items
            ]
            xml = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Fake</title>'
            xml += "".join(parts) + "</feed>"
            content_type = "application/atom+xml"
        else:
            parts = [
                "<item>"
                f"<title>{escape(e['title'])}</title>"
                f"<link>{escape(e['link'])}</link>"
                f"<description>{escape(e.get('summary', ''))}</description>"
                f"<pubDate>{format_datetime(e['published'])}</pubDate>"
                "</item>"
                for e in items
            ]
            xml = '<?xml version="1.0"?><rss version="2.0"><channel><title>Fake</title>'
            xml += "".join(parts) + "</channel></rss>"
            content_type = "application/rss+xml"
        headers = {"etag": etag} if etag else {}
        self.page(url, xml, content_type=content_type, headers=headers)

    def hn(self, stories: Iterable[dict[str, Any]]) -> None:
        """Hacker News top stories: {id, title, url, score, time (datetime), comments}."""
        listed = list(stories)
        base = "https://hacker-news.firebaseio.com/v0"
        self.page(f"{base}/topstories.json", json.dumps([s["id"] for s in listed]), **_JSON)
        for story in listed:
            item = {
                "id": story["id"],
                "type": story.get("type", "story"),
                "title": story["title"],
                "score": story.get("score", 300),
                "descendants": story.get("comments", 40),
                "time": int(story["time"].timestamp()),
            }
            if story.get("url"):
                item["url"] = story["url"]
            self.page(f"{base}/item/{story['id']}.json", json.dumps(item), **_JSON)

    def github_rising(self, repos: Iterable[dict[str, Any]]) -> None:
        body = {"total_count": 0, "items": list(repos)}
        self.handlers["api.github.com/search/repositories"] = lambda _r, _q: _json(body)

    def advisories(self, advisories: Iterable[dict[str, Any]]) -> None:
        """GitHub advisories; filtered by `ecosystem` and `affects` like the real API."""
        stored = list(advisories)

        def handle(_request: Request, query: dict[str, list[str]]) -> Response:
            ecosystem = (query.get("ecosystem") or [""])[0]
            wanted = set(",".join(query.get("affects") or []).split(","))
            found = [
                a
                for a in stored
                if any(
                    v["package"]["ecosystem"] == ecosystem and v["package"]["name"] in wanted
                    for v in a["vulnerabilities"]
                )
            ]
            return _json(found)

        self.handlers["api.github.com/advisories"] = handle

    def kev(self, vulnerabilities: Iterable[dict[str, Any]]) -> None:
        body = {"title": "CISA Catalog of Known Exploited Vulnerabilities", "vulnerabilities": []}
        body["vulnerabilities"] = list(vulnerabilities)
        url = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
        self.page(url, json.dumps(body), **_JSON)

    def searxng(self, results: Iterable[dict[str, Any]], *, host: str = "searxng") -> list[str]:
        """SearXNG's JSON API (at http://searxng:8080). Returns the list of questions asked."""
        listed = list(results)
        asked: list[str] = []

        def handle(_request: Request, query: dict[str, list[str]]) -> Response:
            if (query.get("format") or [""])[0] != "json":
                return Response("JSON isn't switched on", status_code=403)
            question = (query.get("q") or [""])[0]
            asked.append(question)
            return _json({"query": question, "number_of_results": len(listed), "results": listed})

        self.handlers[f"{host}/search"] = handle
        return asked

    # --- The fake servers -------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI()
        web = self

        @app.api_route("/{path:path}", methods=["GET", "HEAD"])
        async def serve(request: Request, path: str) -> Response:
            host = request.headers.get("host", "").split(":")[0].lower()
            query = request.url.query
            where = f"{host}/{path}"
            web.requests.append(where + (f"?{query}" if query else ""))
            handler = web.handlers.get(where)
            if handler is not None:
                return handler(request, parse_qs(query))
            page = web.pages.get(f"{where}?{query}") if query else None
            page = page or web.pages.get(where)
            if page is None:
                return Response("Not found", status_code=404)
            if page.delay:
                await asyncio.sleep(page.delay)
            etag = page.headers.get("etag")
            if etag and request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers={"etag": etag})
            return Response(
                page.body,
                status_code=page.status,
                media_type=page.content_type,
                headers=page.headers,
            )

        return app


_JSON: dict[str, Any] = {"content_type": "application/json"}


def _json(body: Any) -> Response:
    return Response(json.dumps(body), media_type="application/json")


def rss_date(moment: datetime) -> str:
    return format_datetime(moment)
