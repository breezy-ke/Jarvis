"""Reading the brief's sources every hour, so the morning brief has it all already.

A source that fails is retried next round; its health (last success, last
error, failures in a row) shows on the Brief page and in `make doctor`. A story
seen in two places is stored once: a Hacker News post and the article it links
to share a link, so the article gains the points and the discussion link.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update

from jarvis.brief.config import BriefConfig, Source, SourcesConfig
from jarvis.brief.feeds import FeedError
from jarvis.brief.links import link_key
from jarvis.brief.sources import NewItem, SourceError, SourceRead, read_source
from jarvis.db.models import BriefItem, BriefSource
from jarvis.db.session import transaction
from jarvis.security.fetch import FetchError, SafeFetcher
from jarvis.services import Services

log = logging.getLogger("jarvis.brief")

TEXT_KEEP = timedelta(days=7)
DUE_SLACK = timedelta(minutes=5)  # an hourly job that runs a little early still counts


@dataclass
class ReadReport:
    read: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    not_due: list[str] = field(default_factory=list)
    new_items: int = 0


class BriefReader:
    def __init__(
        self,
        services: Services,
        *,
        config: BriefConfig,
        sources: SourcesConfig,
        fetcher: SafeFetcher,
    ) -> None:
        self._s = services
        self._config = config
        self._sources = sources
        self._fetcher = fetcher
        self._store_lock = asyncio.Lock()  # two sources may bring the same new story at once

    async def _stacks(self) -> list[str]:
        async with self._s.session_factory() as session:
            engineering = (await self._s.profiles.current(session)).profile.engineering
        return [*engineering.primary_stacks, *engineering.also_uses]

    async def read_round(self, *, force: bool = False) -> ReadReport:
        """Read every source that's due (all of them with `force`)."""
        now = self._s.clock.now()
        since = now - timedelta(hours=self._config.max_window_hours)
        async with self._s.session_factory() as session:
            states = {row.id: row for row in await session.scalars(select(BriefSource))}
        stacks = await self._stacks()
        token = self._s.settings.github_token
        report = ReadReport()
        gate = asyncio.Semaphore(4)  # different sites in parallel; each site one at a time

        async def one(source: Source) -> None:
            state = states.get(source.id)
            last = state.last_checked_at if state else None
            due = last is None or now - last >= timedelta(hours=source.every_hours) - DUE_SLACK
            if not (force or due):
                report.not_due.append(source.id)
                return
            async with gate:
                try:
                    read = await read_source(
                        source,
                        self._fetcher,
                        now=now,
                        since=since,
                        stacks=stacks,
                        github_token=token.get_secret_value() if token else None,
                        etag=state.etag if state else None,
                        modified=state.last_modified if state else None,
                    )
                except (FetchError, FeedError, SourceError, ValueError, KeyError, TypeError) as exc:
                    reason = f"{type(exc).__name__}: {exc}"[:300]
                    log.info("brief: couldn't read %s: %s", source.id, reason)
                    report.failed[source.id] = reason
                    await self._record(source.id, now, None, reason)
                    return
            added = await self._store(source, read.items, now=now, since=since)
            report.read.append(source.id)
            report.new_items += added
            await self._record(source.id, now, read, None, added=added)

        await asyncio.gather(*(one(source) for source in self._sources.enabled))
        return report

    async def _store(
        self, source: Source, items: list[NewItem], *, now: datetime, since: datetime
    ) -> int:
        fresh: dict[str, NewItem] = {}
        for item in items:
            published = min(item.published, now)  # a date in the future isn't news yet
            if published < since or not item.title.strip():
                continue
            item.published = published
            fresh.setdefault(link_key(item.url), item)
        if not fresh:
            return 0
        async with self._store_lock, transaction(self._s.session_factory) as session:
            known = {
                row.key: row
                for row in await session.scalars(
                    select(BriefItem).where(BriefItem.key.in_(list(fresh))).with_for_update()
                )
            }
            for key, item in fresh.items():
                row = known.get(key)
                if row is not None:  # the same story from another source: combine what's new
                    row.extra = {**row.extra, **item.extra}
                    if not row.summary and item.summary:
                        row.summary = item.summary
            new = [(key, item) for key, item in fresh.items() if key not in known]
            vectors = await self._s.embedder.embed(
                [f"{item.title}. {item.summary[:500]}" for _, item in new]
            )
            for (key, item), vector in zip(new, vectors, strict=True):
                session.add(
                    BriefItem(
                        key=key,
                        url=item.url,
                        source_id=source.id,
                        kind=item.kind,
                        title=item.title,
                        summary=item.summary,
                        published_at=item.published,
                        fetched_at=now,
                        extra=item.extra,
                        embedding=vector,
                    )
                )
        return len(new)

    async def _record(
        self,
        source_id: str,
        now: datetime,
        read: SourceRead | None,
        error: str | None,
        *,
        added: int = 0,
    ) -> None:
        async with transaction(self._s.session_factory) as session:
            state = await session.get(BriefSource, source_id, with_for_update=True)
            if state is None:
                state = BriefSource(id=source_id, failures=0, items_seen=0)
                session.add(state)
            state.last_checked_at = now
            if read is None:
                state.failures = (state.failures or 0) + 1
                state.last_error = error
                return
            state.failures, state.last_error, state.last_ok_at = 0, None, now
            state.items_seen = (state.items_seen or 0) + added
            if not read.not_modified:
                state.etag, state.last_modified = read.etag, read.last_modified

    async def purge(self) -> int:
        """Forget stories past `keep_days`, and article text after a week."""
        now = self._s.clock.now()
        async with transaction(self._s.session_factory) as session:
            await session.execute(
                update(BriefItem)
                .where(BriefItem.text.is_not(None))
                .where(BriefItem.fetched_at < now - TEXT_KEEP)
                .values(text=None)
            )
            result = await session.execute(
                delete(BriefItem).where(
                    BriefItem.published_at < now - timedelta(days=self._config.keep_days)
                )
            )
        return int(getattr(result, "rowcount", 0) or 0)
