"""Searching the web through SearXNG, the private metasearch engine running on your PC.

SearXNG asks the search engines for Jarvis without cookies or your identity.
It runs on Jarvis's own network, so it's called directly, not through the safe
fetcher (which refuses private addresses). Its results are someone else's
words: titles and snippets are cleaned here and treated as untrusted later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from jarvis.brief.feeds import clean_summary, clean_title
from jarvis.brief.links import clean_url, link_key


class SearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    engines: tuple[str, ...] = ()


class SearXNG:
    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 12.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # trust_env=False: SearXNG is on Jarvis's own network, never behind a proxy.
        self._http = http or httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._owned = http is None

    async def aclose(self) -> None:
        if self._owned:
            await self._http.aclose()

    async def search(self, query: str, *, limit: int = 8) -> list[SearchResult]:
        """The best results for `query`: http(s) links only, each page once."""
        params = {"q": query, "format": "json", "safesearch": 1, "categories": "general"}
        try:
            response = await self._http.get(f"{self.base_url}/search", params=params)
        except httpx.HTTPError as exc:
            raise SearchError(
                f"the search engine on your PC didn't answer ({type(exc).__name__})"
            ) from exc
        if response.status_code != 200:
            raise SearchError(f"the search engine on your PC answered {response.status_code}")
        try:
            data: Any = response.json()
        except ValueError as exc:
            raise SearchError("the search engine sent something that isn't JSON") from exc
        results: list[SearchResult] = []
        seen: set[str] = set()
        listed = data.get("results") if isinstance(data, dict) else None
        for raw in listed if isinstance(listed, list) else []:
            if not isinstance(raw, dict):
                continue
            url = clean_url(str(raw.get("url") or ""))
            title = clean_title(str(raw.get("title") or ""))
            if not url.startswith(("https://", "http://")) or not title:
                continue
            key = link_key(url)
            if key in seen:
                continue
            seen.add(key)
            engines = raw.get("engines") or [raw.get("engine")]
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=clean_summary(str(raw.get("content") or ""), limit=500),
                    engines=tuple(str(e) for e in engines if e),
                )
            )
            if len(results) >= limit:
                break
        return results

    async def healthy(self) -> str | None:
        """None if SearXNG is up (without asking any search engine); otherwise what's wrong."""
        try:
            response = await self._http.get(f"{self.base_url}/healthz")
        except httpx.HTTPError as exc:
            return f"the search engine on your PC didn't answer ({type(exc).__name__})"
        return None if response.status_code == 200 else f"it answered {response.status_code}"

    async def reachable(self) -> str | None:
        """None if SearXNG answers searches in JSON; otherwise what's wrong."""
        try:
            await self.search("jarvis", limit=1)
        except SearchError as exc:
            return str(exc)
        return None
