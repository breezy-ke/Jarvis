"""Background work: the action executor loop, plus durable scheduled jobs (DBOS).

* The **executor loop** runs every 2 seconds and executes approved actions
  whose undo window has passed. A plain asyncio task is enough: all its state
  lives in the policy engine's tables, so a restart loses nothing.
* **Scheduled jobs** use DBOS on the same Postgres. Schedules follow your
  local timezone, and DBOS records each run, so a job interrupted by a power
  cut is resumed when Jarvis comes back. They include the inbox digests (at
  the times in email.yaml) and, nightly, forgetting old email text.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete

from jarvis.agents.extraction import build_extraction_agent, extract_idle_conversations
from jarvis.db.models import AuthChallenge, AuthSession, OAuthPending
from jarvis.db.session import transaction
from jarvis.mail.digest import digest_kind
from jarvis.services import Services

if TYPE_CHECKING:
    from jarvis.mail.service import MailService

log = logging.getLogger("jarvis.scheduler")

_services: Services | None = None
_mail: MailService | None = None


def _svc() -> Services:
    assert _services is not None, "scheduler not started"
    return _services


async def run_memory_extraction() -> int:
    services = _svc()
    return await extract_idle_conversations(services, build_extraction_agent())


async def run_housekeeping() -> dict[str, int]:
    services = _svc()
    now = services.clock.now()
    async with transaction(services.session_factory) as session:
        expired = await services.policy.expire_stale(session)
        challenges = await session.execute(
            delete(AuthChallenge).where(AuthChallenge.expires_at < now)
        )
        sessions = await session.execute(delete(AuthSession).where(AuthSession.expires_at < now))
        pending = await session.execute(delete(OAuthPending).where(OAuthPending.expires_at < now))
    return {
        "expired_actions": expired,
        "challenges": challenges.rowcount or 0,  # type: ignore[attr-defined]
        "sessions": sessions.rowcount or 0,  # type: ignore[attr-defined]
        "oauth_pending": pending.rowcount or 0,  # type: ignore[attr-defined]
    }


async def run_nightly_maintenance() -> dict[str, int]:
    services = _svc()
    async with transaction(services.session_factory) as session:
        purged = await services.memory.purge_forgotten(session, older_than=timedelta(days=7))
    bodies = await _mail.purge_old_bodies() if _mail is not None else 0
    return {"purged_facts": purged, "purged_email_bodies": bodies}


async def run_mail_digest(kind: str, scheduled_for: datetime) -> bool:
    if _mail is None:
        return False
    return await _mail.alerts.send_digest(kind, scheduled_for=scheduled_for) is not None


async def executor_loop(services: Services, stop: asyncio.Event, *, interval: float = 2.0) -> None:
    while not stop.is_set():
        try:
            await services.policy.execute_due(services.session_factory)
        except Exception:  # keep the loop alive; the error is logged and retried next tick
            log.exception("executor tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


class Scheduler:
    """Owns the DBOS runtime and the scheduled jobs."""

    def __init__(self, services: Services, *, mail: MailService | None = None) -> None:
        self._services = services
        self._mail = mail
        self._started = False

    async def start(self) -> None:
        global _services, _mail
        from dbos import DBOS, DBOSConfig, ScheduleInput

        _services = self._services
        _mail = self._mail
        config: DBOSConfig = {
            "name": "jarvis",
            "system_database_url": self._services.settings.sync_database_url,
            "log_level": "WARNING",
        }
        DBOS(config=config)

        @DBOS.workflow(name="memory_extraction_job")
        async def memory_extraction_job(scheduled_time: datetime, context: Any) -> None:
            await DBOS.run_step_async({"name": "extract"}, run_memory_extraction)

        @DBOS.workflow(name="housekeeping_job")
        async def housekeeping_job(scheduled_time: datetime, context: Any) -> None:
            await DBOS.run_step_async({"name": "housekeeping"}, run_housekeeping)

        @DBOS.workflow(name="nightly_maintenance_job")
        async def nightly_maintenance_job(scheduled_time: datetime, context: Any) -> None:
            await DBOS.run_step_async({"name": "maintenance"}, run_nightly_maintenance)

        @DBOS.workflow(name="mail_digest_job")
        async def mail_digest_job(scheduled_time: datetime, context: Any) -> None:
            await DBOS.run_step_async(
                {"name": "digest"}, run_mail_digest, str(context), scheduled_time
            )

        DBOS.launch()
        tz = self._services.policies_config.defaults.timezone
        digests: list[ScheduleInput] = []
        if self._mail is not None:
            for moment in self._mail.config.digests.clock_times:
                digests.append(
                    {
                        "schedule_name": f"mail-digest-{moment:%H%M}",
                        "workflow_fn": mail_digest_job,
                        "schedule": f"{moment.minute} {moment.hour} * * *",
                        "context": digest_kind(moment),
                        "automatic_backfill": True,  # a late digest is sent, a stale one skipped
                        "cron_timezone": tz,
                        "queue_name": None,
                    }
                )
        await DBOS.apply_schedules_async(
            [
                *digests,
                {
                    "schedule_name": "memory-extraction",
                    "workflow_fn": memory_extraction_job,
                    "schedule": "*/5 * * * *",
                    "context": None,
                    "automatic_backfill": False,
                    "cron_timezone": tz,
                    "queue_name": None,
                },
                {
                    "schedule_name": "housekeeping",
                    "workflow_fn": housekeeping_job,
                    "schedule": "17 * * * *",
                    "context": None,
                    "automatic_backfill": False,
                    "cron_timezone": tz,
                    "queue_name": None,
                },
                {
                    "schedule_name": "nightly-maintenance",
                    "workflow_fn": nightly_maintenance_job,
                    "schedule": "0 3 * * *",
                    "context": None,
                    "automatic_backfill": True,  # catch up after a night offline
                    "cron_timezone": tz,
                    "queue_name": None,
                },
            ]
        )
        self._started = True
        log.info("scheduler started (timezone %s)", tz)

    async def stop(self) -> None:
        if self._started:
            from dbos import DBOS

            DBOS.destroy()
            self._started = False
