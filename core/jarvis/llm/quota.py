"""Free-tier quota tracking and the paid-provider budget.

Every model call is written to `llm_calls`. Before a call, the router asks
whether a model still has headroom in its per-minute and per-day windows. At
`quota_headroom` (for example 90%) of any limit it switches to the next
candidate, so requests are not rejected mid-conversation. Day windows are
rolling 24 hours: stricter than a provider's midnight reset, never looser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import LLMCall
from jarvis.llm.config import Limits


@dataclass(frozen=True)
class WindowUsage:
    requests: int
    tokens: int


async def usage_since(session: AsyncSession, model_ref: str, since: datetime) -> WindowUsage:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(LLMCall.requests), 0),
                func.coalesce(func.sum(LLMCall.input_tokens + LLMCall.output_tokens), 0),
            )
            .where(LLMCall.model_ref == model_ref)
            .where(LLMCall.ts >= since)
        )
    ).one()
    return WindowUsage(requests=int(row[0]), tokens=int(row[1]))


async def quota_blocker(
    session: AsyncSession,
    *,
    model_ref: str,
    limits: Limits,
    now: datetime,
    est_tokens: int,
    headroom: float,
) -> str | None:
    """Return why this model should be skipped right now, or None if it's fine."""
    if not any((limits.rpm, limits.rpd, limits.tpm, limits.tpd)):
        return None
    minute = await usage_since(session, model_ref, now - timedelta(minutes=1))
    day = await usage_since(session, model_ref, now - timedelta(days=1))
    checks = (
        (limits.rpm, minute.requests + 1, "requests per minute"),
        (limits.rpd, day.requests + 1, "requests per day"),
        (limits.tpm, minute.tokens + est_tokens, "tokens per minute"),
        (limits.tpd, day.tokens + est_tokens, "tokens per day"),
    )
    for limit, projected, label in checks:
        if limit is not None and projected > limit * headroom:
            return f"near its free-tier limit of {limit} {label}"
    return None


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_spend(session: AsyncSession, providers: list[str], now: datetime) -> float:
    if not providers:
        return 0.0
    total = await session.scalar(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
        .where(LLMCall.provider.in_(providers))
        .where(LLMCall.ts >= month_start(now))
    )
    return float(total or 0.0)
