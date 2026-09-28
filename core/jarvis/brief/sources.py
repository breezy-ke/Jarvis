"""Reading each kind of source: feeds, Hacker News, GitHub and CISA.

Every read goes through `SafeFetcher`. What comes back is public data, and
untrusted: it's stored as text and never followed as instructions.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from jarvis.brief.config import (
    AdvisorySource,
    FeedSource,
    GitHubRisingSource,
    HNSource,
    KEVSource,
    Source,
)
from jarvis.brief.feeds import clean_summary, clean_title, parse_date, parse_feed
from jarvis.brief.links import clean_url
from jarvis.security.fetch import Fetched, FetchError, SafeFetcher

HN_API = "https://hacker-news.firebaseio.com/v0"
GITHUB_API = "https://api.github.com"
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"

# Your stacks (from your profile) → the packages whose advisories you'd want.
STACK_PACKAGES: tuple[tuple[re.Pattern[str], dict[str, list[str]]], ...] = (
    (re.compile(r"\bnext(\.?js)?\b"), {"npm": ["next"]}),
    (re.compile(r"\breact[ -]?native\b"), {"npm": ["react-native"]}),
    (re.compile(r"\breact(?![ -]?native)\b"), {"npm": ["react", "react-dom"]}),
    (re.compile(r"\bnuxt\b"), {"npm": ["nuxt"]}),
    (re.compile(r"\bvue\b"), {"npm": ["vue"]}),
    (re.compile(r"\bsvelte(kit)?\b"), {"npm": ["svelte", "@sveltejs/kit"]}),
    (re.compile(r"\bangular\b"), {"npm": ["@angular/core"]}),
    (re.compile(r"\bexpress\b"), {"npm": ["express"]}),
    (re.compile(r"\btailwind"), {"npm": ["tailwindcss"]}),
    (re.compile(r"\bvite\b"), {"npm": ["vite"]}),
    (re.compile(r"\blaravel\b"), {"composer": ["laravel/framework"]}),
    (re.compile(r"\bsymfony\b"), {"composer": ["symfony/http-kernel"]}),
    (re.compile(r"\bdjango\b"), {"pip": ["django"]}),
    (re.compile(r"\bflask\b"), {"pip": ["flask"]}),
    (re.compile(r"\bfastapi\b"), {"pip": ["fastapi", "starlette"]}),
)


class SourceError(RuntimeError):
    pass


@dataclass
class NewItem:
    url: str
    title: str
    summary: str
    published: datetime
    kind: str = "story"  # story, repo, advisory, exploited
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceRead:
    items: list[NewItem] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


def packages_for(stacks: list[str], extra: dict[str, list[str]]) -> dict[str, list[str]]:
    """Advisory packages by ecosystem: what your stacks imply, plus the ones you listed."""
    found: dict[str, list[str]] = {}
    for stack in stacks:
        for pattern, packages in STACK_PACKAGES:
            if pattern.search(stack.lower()):
                for ecosystem, names in packages.items():
                    found.setdefault(ecosystem, []).extend(names)
    for ecosystem, names in extra.items():
        found.setdefault(ecosystem, []).extend(names)
    return {eco: sorted(set(names)) for eco, names in found.items() if names}


def _json(fetched: Fetched) -> Any:
    try:
        return json.loads(fetched.body)
    except ValueError as exc:
        raise SourceError("the answer wasn't JSON") from exc


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def read_feed(
    source: FeedSource,
    fetcher: SafeFetcher,
    *,
    now: datetime,
    etag: str | None,
    modified: str | None,
) -> SourceRead:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    fetched = await fetcher.get(source.url, headers=headers, accept=FEED_ACCEPT)
    if fetched.not_modified:
        return SourceRead(etag=etag, last_modified=modified, not_modified=True)
    entries = parse_feed(fetched.body, base_url=fetched.url)
    items = [
        NewItem(url=e.url, title=e.title, summary=e.summary, published=e.published or now)
        for e in entries
    ]
    return SourceRead(
        items=items,
        etag=fetched.headers.get("etag"),
        last_modified=fetched.headers.get("last-modified"),
    )


async def read_hn(source: HNSource, fetcher: SafeFetcher, *, now: datetime) -> SourceRead:
    top = _json(
        await fetcher.get(f"{HN_API}/topstories.json", pace=0.05, accept="application/json")
    )
    ids = [i for i in top if isinstance(i, int)][: source.stories]

    async def story(story_id: int) -> dict[str, Any] | None:
        try:
            data = _json(await fetcher.get(f"{HN_API}/item/{story_id}.json", pace=0.05))
        except (FetchError, SourceError):
            return None
        return data if isinstance(data, dict) else None

    items: list[NewItem] = []
    for data in await asyncio.gather(*(story(i) for i in ids)):
        if not data or data.get("type") != "story" or data.get("dead") or data.get("deleted"):
            continue
        points = int(data.get("score") or 0)
        if points < source.min_points or not data.get("title"):
            continue
        discussion = f"https://news.ycombinator.com/item?id={int(data['id'])}"
        url = str(data.get("url") or discussion)
        if not url.startswith(("https://", "http://")):
            continue
        items.append(
            NewItem(
                url=clean_url(url),
                title=clean_title(str(data["title"])),
                summary="",
                published=datetime.fromtimestamp(int(data.get("time") or now.timestamp()), UTC),
                extra={
                    "hn": {
                        "points": points,
                        "comments": int(data.get("descendants") or 0),
                        "discussion": discussion,
                    }
                },
            )
        )
    return SourceRead(items=items)


async def read_github_rising(
    source: GitHubRisingSource, fetcher: SafeFetcher, *, now: datetime, token: str | None
) -> SourceRead:
    since = (now - timedelta(days=source.days)).date().isoformat()
    query = urlencode(
        {
            "q": f"created:>={since} stars:>={source.min_stars}",
            "sort": "stars",
            "order": "desc",
            "per_page": 20,
        }
    )
    data = _json(
        await fetcher.get(
            f"{GITHUB_API}/search/repositories?{query}",
            headers=_github_headers(token),
            accept="application/vnd.github+json",
            pace=0.5,
        )
    )
    items: list[NewItem] = []
    for repo in data.get("items") or []:
        name, url = str(repo.get("full_name") or ""), str(repo.get("html_url") or "")
        if not name or not url.startswith("https://github.com/"):
            continue
        description = clean_summary(str(repo.get("description") or ""), limit=300)
        items.append(
            NewItem(
                url=url,
                title=clean_title(f"{name}: {description}" if description else name),
                summary=description,
                published=parse_date(str(repo.get("created_at") or "")) or now,
                kind="repo",
                extra={
                    "repo": {
                        "name": name,
                        "stars": int(repo.get("stargazers_count") or 0),
                        "language": repo.get("language"),
                    }
                },
            )
        )
    return SourceRead(items=items)


async def read_advisories(
    source: AdvisorySource,
    fetcher: SafeFetcher,
    *,
    now: datetime,
    since: datetime,
    stacks: list[str],
    token: str | None,
) -> SourceRead:
    wanted = packages_for(stacks, {k: list(v) for k, v in source.packages.items()})
    items: list[NewItem] = []
    for ecosystem, names in wanted.items():
        query = urlencode(
            {
                "type": "reviewed",
                "ecosystem": ecosystem,
                "affects": ",".join(names),
                "published": f">={since.date().isoformat()}",
                "per_page": 100,
            }
        )
        data = _json(
            await fetcher.get(
                f"{GITHUB_API}/advisories?{query}",
                headers=_github_headers(token),
                accept="application/vnd.github+json",
                pace=0.5,
            )
        )
        for advisory in data if isinstance(data, list) else []:
            severity = str(advisory.get("severity") or "unknown")
            url = str(advisory.get("html_url") or "")
            if severity not in source.severities or not url.startswith("https://github.com/"):
                continue
            affected = [
                {
                    "ecosystem": ecosystem,
                    "name": str(v.get("package", {}).get("name") or ""),
                    "vulnerable": str(v.get("vulnerable_version_range") or ""),
                    "patched": str(v.get("first_patched_version") or ""),
                }
                for v in advisory.get("vulnerabilities") or []
                if str(v.get("package", {}).get("name") or "") in names
            ]
            if not affected:
                continue
            summary = clean_title(str(advisory.get("summary") or "Security advisory"))
            items.append(
                NewItem(
                    url=url,
                    title=f"{affected[0]['name']}: {summary}"[:300],
                    summary=clean_summary(str(advisory.get("description") or ""), limit=600),
                    published=parse_date(str(advisory.get("published_at") or "")) or now,
                    kind="advisory",
                    extra={
                        "advisory": {
                            "ghsa": advisory.get("ghsa_id"),
                            "cve": advisory.get("cve_id"),
                            "severity": severity,
                            "packages": affected,
                        }
                    },
                )
            )
    return SourceRead(items=items)


async def read_kev(
    source: KEVSource, fetcher: SafeFetcher, *, now: datetime, since: datetime
) -> SourceRead:
    data = _json(await fetcher.get(source.url, max_bytes=8_000_000, accept="application/json"))
    terms = [t.lower() for t in source.match]
    items: list[NewItem] = []
    for entry in data.get("vulnerabilities") or []:
        added = parse_date(f"{entry.get('dateAdded') or ''}T00:00:00+00:00")
        cve = str(entry.get("cveID") or "")
        if added is None or added < since.replace(hour=0, minute=0, second=0, microsecond=0):
            continue
        product = f"{entry.get('vendorProject') or ''} {entry.get('product') or ''}".strip()
        if terms and not any(term in product.lower() for term in terms):
            continue
        if not re.fullmatch(r"CVE-\d{4}-\d{4,}", cve):
            continue
        items.append(
            NewItem(
                url=f"https://nvd.nist.gov/vuln/detail/{cve}",
                title=clean_title(f"{product}: {entry.get('vulnerabilityName') or cve}"),
                summary=clean_summary(str(entry.get("shortDescription") or ""), limit=600),
                published=added,
                kind="exploited",
                extra={
                    "exploited": {
                        "cve": cve,
                        "product": product,
                        "action": str(entry.get("requiredAction") or "")[:300],
                        "due": entry.get("dueDate"),
                        "ransomware": entry.get("knownRansomwareCampaignUse") == "Known",
                    }
                },
            )
        )
    return SourceRead(items=items)


def probe_address(source: Source) -> str:
    """One address that shows whether a source is reachable (for `make doctor ONLINE=1`)."""
    if isinstance(source, FeedSource | KEVSource):
        return source.url
    if isinstance(source, HNSource):
        return f"{HN_API}/topstories.json"
    if isinstance(source, GitHubRisingSource):
        return f"{GITHUB_API}/search/repositories?q=stars%3A%3E1000&per_page=1"
    return f"{GITHUB_API}/advisories?per_page=1"


async def read_source(
    source: Source,
    fetcher: SafeFetcher,
    *,
    now: datetime,
    since: datetime,
    stacks: list[str],
    github_token: str | None,
    etag: str | None = None,
    modified: str | None = None,
) -> SourceRead:
    if isinstance(source, FeedSource):
        return await read_feed(source, fetcher, now=now, etag=etag, modified=modified)
    if isinstance(source, HNSource):
        return await read_hn(source, fetcher, now=now)
    if isinstance(source, GitHubRisingSource):
        return await read_github_rising(source, fetcher, now=now, token=github_token)
    if isinstance(source, AdvisorySource):
        return await read_advisories(
            source, fetcher, now=now, since=since, stacks=stacks, token=github_token
        )
    return await read_kev(source, fetcher, now=now, since=since)
