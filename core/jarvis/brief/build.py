"""Putting a morning's brief together.

1. The window: everything since the last brief, at most `max_window_hours`,
   and never a story an earlier brief already had.
2. Ranking by code (rank.py); the security watch is sorted by how urgent it is.
3. The articles themselves are read (robots.txt permitting) for the summaries.
4. A public model summarises the articles: it sees public text only.
5. A private model adds why each story matters to you, one thing to do today
   and the spoken version: it's the only one that sees your profile.
6. If no model can help, the brief still goes out: titles, the sources' own
   summaries, links, and the security watch (which is all code anyway).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select

from jarvis.brief.agents import (
    Notes,
    Summaries,
    build_notes_agent,
    build_summary_agent,
    no_links,
)
from jarvis.brief.config import BriefConfig, SourcesConfig
from jarvis.brief.rank import (
    Scored,
    Taste,
    interest_text,
    merge_same_stories,
    score,
    stack_terms,
)
from jarvis.db.models import Brief, BriefEntry, BriefItem
from jarvis.db.session import transaction
from jarvis.llm.router import RouterError
from jarvis.profile.schema import Profile
from jarvis.profile.service import core_summary
from jarvis.security.fetch import FetchError, RobotsCache, SafeFetcher
from jarvis.security.untrusted import wrap
from jarvis.services import Services

log = logging.getLogger("jarvis.brief")

SECURITY_KINDS = frozenset({"advisory", "exploited"})
TASTE_DAYS = 60
SUMMARY_BATCH = 5
ARTICLE_CHARS = 20_000
NOT_FOR_NOTES = ("personal", "people", "boundaries", "schedule")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_SEVERITY = {"critical": 1, "high": 2, "medium": 3, "low": 4}


def first_sentences(text: str, count: int = 2, limit: int = 400) -> str:
    sentences = [s for s in _SENTENCE.split(text.strip()) if s]
    joined = " ".join(sentences[:count])
    return joined if len(joined) <= limit else joined[: limit - 1].rstrip() + "…"


def urgency(scored: Scored) -> tuple[int, float]:
    """Exploited-in-the-wild first, then by severity; newest first within each."""
    extra = scored.item.extra or {}
    level = (
        0
        if scored.item.kind == "exploited"
        else _SEVERITY.get(str(extra.get("advisory", {}).get("severity")), 5)
    )
    return level, -scored.item.published_at.timestamp()


def security_details(item: BriefItem) -> dict[str, Any]:
    extra = item.extra or {}
    if item.kind == "exploited":
        return {"exploited": extra.get("exploited", {})}
    return {"advisory": extra.get("advisory", {})}


def story_details(item: BriefItem) -> dict[str, Any]:
    extra = item.extra or {}
    details: dict[str, Any] = {}
    if "hn" in extra:
        details["hn"] = extra["hn"]
    if "repo" in extra:
        details["repo"] = extra["repo"]
    return details


@dataclass
class Plan:
    """What a brief will hold, before any model writes a word."""

    top: list[Scored] = field(default_factory=list)
    security: list[Scored] = field(default_factory=list)
    africa: list[Scored] = field(default_factory=list)
    quick: list[Scored] = field(default_factory=list)

    @property
    def stories(self) -> list[Scored]:
        return [*self.top, *self.africa, *self.quick]


class BriefBuilder:
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
        self._robots = RobotsCache(fetcher)
        self._summaries = build_summary_agent()
        self._notes = build_notes_agent(minutes=config.audio_minutes)
        self._lock = asyncio.Lock()  # one brief at a time: the 06:40 run and "make it now"

    # --- Helpers --------------------------------------------------------------------------

    def scheduled_for(self, day: date) -> datetime:
        return datetime.combine(day, self._config.clock_time, tzinfo=self._s.policies_config.tz)

    def source_name(self, source_id: str) -> str:
        source = self._sources.get(source_id)
        return source.name if source else source_id

    def _category(self, source_id: str) -> str:
        source = self._sources.get(source_id)
        return source.category if source else "news"

    def _weight(self, source_id: str) -> float:
        source = self._sources.get(source_id)
        return source.weight if source else 1.0

    def enc(self, value: Any) -> bytes:
        return self._s.vault.encrypt(json.dumps(value, ensure_ascii=False))

    def dec(self, blob: bytes | None, default: Any) -> Any:
        return default if blob is None else json.loads(self._s.vault.decrypt_str(blob))

    # --- Choosing ---------------------------------------------------------------------------

    async def window(self, day: date) -> tuple[datetime, datetime]:
        """Since the last brief you got, but at most `max_window_hours` back."""
        now = self._s.clock.now()
        earliest = now - timedelta(hours=self._config.max_window_hours)
        async with self._s.session_factory() as session:
            last_end = await session.scalar(
                select(Brief.window_end)
                .where(Brief.day < day, Brief.status == "delivered")
                .order_by(Brief.day.desc())
                .limit(1)
            )
        return max(last_end or earliest, earliest), now

    async def skip_stale(self, day: date) -> int:
        """Earlier briefs that never went out won't now: their stories stay available."""
        async with transaction(self._s.session_factory) as session:
            stale = list(
                await session.scalars(
                    select(Brief).where(Brief.day < day, Brief.status == "ready").with_for_update()
                )
            )
            for brief in stale:
                brief.status = "skipped"
                brief.updated_at = self._s.clock.now()
        return len(stale)

    async def taste(self) -> Taste:
        since = self._s.clock.now() - timedelta(days=TASTE_DAYS)
        async with self._s.session_factory() as session:
            rows = (
                await session.execute(
                    select(BriefEntry.source_id, BriefEntry.vote, BriefEntry.embedding)
                    .where(BriefEntry.vote.is_not(None))
                    .where(BriefEntry.voted_at >= since)
                    .order_by(BriefEntry.voted_at.desc())
                    .limit(300)
                )
            ).all()
        return Taste.from_votes((s, int(v), e) for s, v, e in rows)

    async def plan(self, start: datetime, end: datetime, profile: Profile) -> Plan:
        earliest = end - timedelta(hours=self._config.max_window_hours)
        async with self._s.session_factory() as session:
            used = (  # stories a brief you got already had
                select(BriefEntry.item_id)
                .join(Brief, Brief.id == BriefEntry.brief_id)
                .where(BriefEntry.item_id.is_not(None), Brief.status == "delivered")
            )
            items = list(
                await session.scalars(
                    select(BriefItem)
                    .where((BriefItem.published_at >= start) | (BriefItem.fetched_at >= start))
                    .where(BriefItem.published_at >= earliest)
                    .where(BriefItem.id.not_in(used))
                )
            )
        interest_words = interest_text(profile)
        interest = (await self._s.embedder.embed([interest_words]))[0] if interest_words else None
        stacks, taste = stack_terms(profile), await self.taste()
        scored = [
            score(
                item,
                now=end,
                source_weight=self._weight(item.source_id),
                interest=interest,
                stacks=stacks,
                taste=taste,
            )
            for item in items
        ]
        sections = self._config.sections
        security = sorted((s for s in scored if s.item.kind in SECURITY_KINDS), key=urgency)
        stories = merge_same_stories([s for s in scored if s.item.kind not in SECURITY_KINDS])
        top, rest = stories[: sections.top], stories[sections.top :]
        africa = [s for s in rest if self._category(s.item.source_id) == "africa"]
        africa = africa[: sections.africa]
        quick = [s for s in rest if s not in africa][: sections.quick]
        return Plan(top=top, security=security[: sections.security], africa=africa, quick=quick)

    # --- Reading and writing --------------------------------------------------------------------

    async def read_articles(self, stories: list[Scored]) -> None:
        """The article itself, where robots.txt allows, for better summaries."""
        import trafilatura

        gate = asyncio.Semaphore(4)

        async def read(item: BriefItem) -> None:
            host = (urlsplit(item.url).hostname or "").lower()
            if item.text or item.kind != "story" or host == "news.ycombinator.com":
                return
            async with gate:
                try:
                    if not await self._robots.allowed(item.url):
                        return
                    page = await self._fetcher.get(
                        item.url, max_bytes=3_000_000, accept="text/html,application/xhtml+xml"
                    )
                except FetchError:
                    return
            if "html" not in page.content_type:
                return
            text = await asyncio.to_thread(
                trafilatura.extract, page.text(), include_comments=False, include_links=False
            )
            if text:
                item.text = text[:ARTICLE_CHARS]
                async with transaction(self._s.session_factory) as session:
                    row = await session.get(BriefItem, item.id)
                    if row is not None:
                        row.text = item.text

        await asyncio.gather(*(read(s.item) for s in stories))

    def _story_block(self, number: int, item: BriefItem, body: str, *, limit: int) -> str:
        host = urlsplit(item.url).hostname or "?"
        written = f"Title: {item.title}\nSource: {self.source_name(item.source_id)}\n\n{body}"
        return f"{number}.\n" + wrap(
            written, source=f"{host}", kind="news article", max_chars=limit
        )

    async def summarise(self, stories: list[Scored]) -> tuple[dict[uuid.UUID, str], str | None]:
        """Neutral summaries by a public model. Public article text only: no profile."""
        summaries: dict[uuid.UUID, str] = {}
        model: str | None = None
        for start in range(0, len(stories), SUMMARY_BATCH):
            batch = stories[start : start + SUMMARY_BATCH]
            blocks = [
                self._story_block(
                    n, s.item, s.item.text or s.item.summary or s.item.title, limit=4_000
                )
                for n, s in enumerate(batch, 1)
            ]
            prompt = "Stories to summarise:\n\n" + "\n\n".join(blocks)
            try:
                outcome = await self._s.router.run(self._summaries, prompt, task="public_summarize")
            except RouterError as exc:
                log.info("brief: no model for summaries, using the sources' own: %s", exc)
                continue
            result: Summaries = outcome.output
            model = outcome.model_ref
            for entry in result.items:
                if 1 <= entry.index <= len(batch) and entry.summary:
                    summaries[batch[entry.index - 1].item.id] = entry.summary
        for scored in stories:  # whatever a model didn't cover: the source's own words
            if scored.item.id not in summaries:
                summaries[scored.item.id] = first_sentences(
                    scored.item.summary or scored.item.title
                )
        return summaries, model

    async def write_notes(
        self, profile: Profile, plan: Plan, summaries: dict[uuid.UUID, str]
    ) -> tuple[Notes, str | None]:
        """Why it matters to you, and the spoken version: a private model, with your profile."""
        if not plan.top and not plan.security:
            return Notes(), None  # nothing to comment on: no model gets the chance to invent news
        # Only what helps say why a story matters: your contacts, boundaries,
        # schedule and personal notes stay out of it.
        about = core_summary(profile, max_chars=2_000, exclude=NOT_FOR_NOTES)
        stories = "\n\n".join(
            f"{n}.\n"
            + wrap(
                f"{s.item.title}\n{summaries.get(s.item.id, '')}",
                source=self.source_name(s.item.source_id),
                kind="news summary",
                max_chars=1_200,
            )
            for n, s in enumerate(plan.top, 1)
        )
        watch = (
            "\n".join(f"- {line}" for line in security_lines(plan.security)) or "Nothing urgent."
        )
        more = "\n".join(f"- {s.item.title}" for s in [*plan.africa, *plan.quick]) or "-"
        prompt = (
            f"## About the owner\n{about}\n\n## Top stories\n{stories}\n\n"
            f"## Security watch (checked facts)\n{watch}\n\n## Also today (titles)\n{more}"
        )
        try:
            outcome = await self._s.router.run(self._notes, prompt, task="brief")
        except RouterError as exc:
            log.info("brief: no private model for notes: %s", exc)
            return Notes(), None
        notes: Notes = outcome.output
        notes.items = [n for n in notes.items if 1 <= n.index <= len(plan.top)]
        return notes, outcome.model_ref

    # --- The brief ----------------------------------------------------------------------------

    async def prepare(self, day: date) -> Brief:
        """The brief for `day`, built once: one already made is returned as it is."""
        async with self._lock:
            async with self._s.session_factory() as session:
                existing = await session.scalar(select(Brief).where(Brief.day == day))
            if existing is not None and existing.status in ("ready", "delivered", "skipped"):
                return existing
            await self.skip_stale(day)
            return await self._build(day)

    async def _build(self, day: date) -> Brief:
        start, end = await self.window(day)
        async with self._s.session_factory() as session:
            profile = (await self._s.profiles.current(session)).profile
        plan = await self.plan(start, end, profile)
        await self.read_articles(plan.stories)
        summaries, summary_model = await self.summarise(plan.stories)
        notes, notes_model = await self.write_notes(profile, plan, summaries)
        do_today = notes.do_today or fallback_action(plan)
        script = notes.script or fallback_script(plan, summaries, self.source_name)
        now = self._s.clock.now()
        async with transaction(self._s.session_factory) as session:
            brief = await session.scalar(select(Brief).where(Brief.day == day).with_for_update())
            if brief is None:
                brief = Brief(id=uuid.uuid4(), day=day, created_at=now)
                session.add(brief)
            else:  # an unfinished earlier attempt: start its entries afresh
                await session.execute(delete(BriefEntry).where(BriefEntry.brief_id == brief.id))
            brief.status = "ready"
            brief.window_start, brief.window_end = start, end
            brief.scheduled_for = self.scheduled_for(day)
            brief.extra_enc = self.enc({"do_today": do_today, "script": script})
            brief.models = {"summaries": summary_model, "notes": notes_model}
            brief.deliveries = brief.deliveries or {}
            brief.updated_at = now
            await session.flush()
            notes_by_index = {n.index: n for n in notes.items}
            sections = (
                ("top", plan.top),
                ("security", plan.security),
                ("africa", plan.africa),
                ("quick", plan.quick),
            )
            for section, chosen in sections:
                for rank, scored in enumerate(chosen, 1):
                    item = scored.item
                    note = notes_by_index.get(rank) if section == "top" else None
                    session.add(
                        BriefEntry(
                            brief_id=brief.id,
                            item_id=item.id,
                            section=section,
                            rank=rank,
                            source_id=item.source_id,
                            source_name=self.source_name(item.source_id),
                            title=item.title,
                            url=item.url,  # the source's own link, never a model's
                            summary=(
                                first_sentences(item.summary or item.title, 2)
                                if section == "security"
                                else summaries.get(item.id, "")
                            ),
                            note_enc=self.enc({"why": note.why, "client": note.client})
                            if note is not None and note.why
                            else None,
                            details=(
                                security_details(item)
                                if section == "security"
                                else story_details(item)
                            ),
                            why={**scored.why, "also": scored.also} if scored.also else scored.why,
                            embedding=item.embedding,
                        )
                    )
        return brief


def watch_lines(details: dict[str, Any]) -> list[str]:
    """One security-watch entry in a line or two, from the advisory data alone."""
    if "exploited" in details:
        data = details["exploited"]
        return [
            f"{data.get('product')}: {data.get('cve')} is being exploited now. "
            f"{data.get('action') or 'Apply the vendor fix.'}"
        ]
    data = details.get("advisory", {})
    lines: list[str] = []
    for package in data.get("packages", [])[:2]:
        fix = f"fixed in {package['patched']}" if package.get("patched") else "no fix yet"
        affected = f" {package['vulnerable']}" if package.get("vulnerable") else ""
        lines.append(f"{package['name']}{affected}: {data.get('severity')} severity, {fix}.")
    return lines


def security_lines(chosen: list[Scored]) -> list[str]:
    return [line for s in chosen for line in watch_lines(security_details(s.item))]


def fallback_action(plan: Plan) -> str:
    for scored in plan.security:
        data = (scored.item.extra or {}).get("advisory", {})
        for package in data.get("packages", []):
            if package.get("patched"):
                return (
                    f"Check your projects for {package['name']} {package.get('vulnerable')} "
                    f"and update to {package['patched']}."
                )
    if plan.top:
        return f"Read “{plan.top[0].item.title}”."
    return ""


def fallback_script(plan: Plan, summaries: dict[uuid.UUID, str], source_name: Any) -> str:
    """The spoken version when no private model is free: the headlines, read plainly."""
    if not plan.top and not plan.security:
        return (
            "Good morning. It's a quiet morning: nothing new worth your time since the last brief."
        )
    parts = ["Good morning. Here's your tech brief."]
    for n, scored in enumerate(plan.top, 1):
        summary = summaries.get(scored.item.id) or ""
        parts.append(
            f"Number {n}, from {source_name(scored.item.source_id)}: {scored.item.title}. {summary}"
        )
    lines = security_lines(plan.security)
    if lines:
        parts.append("On the security watch. " + " ".join(lines))
    return no_links(" ".join(parts))
