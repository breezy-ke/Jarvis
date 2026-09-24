"""Append-only, hash-chained audit log.

Each event stores the SHA-256 of its canonical JSON body, and that body
includes the previous event's hash. Editing or deleting a past event breaks
every later link, so `verify()` detects it. Appends are serialised with a
transaction-scoped Postgres advisory lock, so two writers can never fork the
chain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.clock import Clock, SystemClock
from jarvis.db.models import AuditEvent
from jarvis.security.hashing import GENESIS_HASH, canonical_json, sha256_hex

_AUDIT_LOCK_KEY = 0x4A41525649  # "JARVI"


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    first_bad_id: int | None = None
    reason: str | None = None


class AuditLog:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()

    async def append(
        self,
        session: AsyncSession,
        *,
        actor: str,
        event_type: str,
        summary: str,
        data: dict[str, Any] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> AuditEvent:
        """Append an event inside the caller's transaction (commit promptly)."""
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _AUDIT_LOCK_KEY})
        prev_hash = await session.scalar(
            select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1)
        )
        ts = self._clock.now()
        body = {
            "ts": ts.isoformat(),
            "actor": actor,
            "event_type": event_type,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "summary": summary,
            "data": data or {},
            "prev_hash": prev_hash or GENESIS_HASH,
        }
        canonical = canonical_json(body)
        event = AuditEvent(
            ts=ts,
            actor=actor,
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            summary=summary,
            data=data or {},
            prev_hash=prev_hash or GENESIS_HASH,
            hash=sha256_hex(canonical),
            canonical=canonical,
        )
        session.add(event)
        await session.flush()
        return event

    async def verify(self, session: AsyncSession, *, batch_size: int = 1000) -> VerifyResult:
        """Walk the whole chain and confirm every hash and every link."""
        expected_prev = GENESIS_HASH
        checked = 0
        last_id = 0
        while True:
            rows = (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.id > last_id)
                    .order_by(AuditEvent.id)
                    .limit(batch_size)
                )
            ).scalars()
            batch = list(rows)
            if not batch:
                return VerifyResult(ok=True, checked=checked)
            for event in batch:
                reason = self._check_event(event, expected_prev)
                if reason:
                    return VerifyResult(
                        ok=False, checked=checked, first_bad_id=event.id, reason=reason
                    )
                expected_prev = event.hash
                last_id = event.id
                checked += 1

    @staticmethod
    def _check_event(event: AuditEvent, expected_prev: str) -> str | None:
        if sha256_hex(event.canonical) != event.hash:
            return "hash does not match the stored body"
        try:
            body = json.loads(event.canonical)
        except ValueError:
            return "stored body is not valid JSON"
        if body.get("prev_hash") != expected_prev or event.prev_hash != expected_prev:
            return "link to the previous event is broken (an event was altered or removed)"
        mirrored = {
            "actor": event.actor,
            "event_type": event.event_type,
            "subject_type": event.subject_type,
            "subject_id": event.subject_id,
            "summary": event.summary,
            "data": event.data,
        }
        for key, value in mirrored.items():
            if body.get(key) != value:
                return f"column '{key}' differs from the hashed body"
        return None
