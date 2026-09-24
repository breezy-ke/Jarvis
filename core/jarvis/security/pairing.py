"""One-time pairing codes for linking a device or account to Jarvis.

The owner makes a code in the app (with a passkey tap) and types or scans it on
the thing being linked: a voice satellite, or their Telegram account. Codes are
short enough to type, unambiguous to read aloud, valid for ten minutes, single
use, stored only as a hash, and guarded against guessing.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.clock import Clock
from jarvis.db.models import SystemState
from jarvis.security.hashing import sha256_hex

PAIRING_TTL = timedelta(minutes=10)
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread
MAX_FAILURES = 10
FAILURE_WINDOW = timedelta(minutes=15)


class PairingError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PairingCode:
    code: str
    expires_at: datetime

    @property
    def compact(self) -> str:
        """The code without its dash (for links)."""
        return canonical(self.code)


def canonical(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


class PairingCodes:
    """At most one live code per purpose, kept in `system_state` under `key`."""

    def __init__(self, key: str, *, clock: Clock, ttl: timedelta = PAIRING_TTL) -> None:
        self._key = key
        self._clock = clock
        self._ttl = ttl
        self._failures: list[datetime] = []

    async def issue(self, session: AsyncSession) -> PairingCode:
        """Make a new code. Any earlier code for the same purpose stops working."""
        raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
        code = f"{raw[:4]}-{raw[4:]}"
        now = self._clock.now()
        expires_at = now + self._ttl
        value = {"hash": sha256_hex(canonical(code)), "expires_at": expires_at.isoformat()}
        row = await session.get(SystemState, self._key, with_for_update=True)
        if row is None:
            session.add(SystemState(key=self._key, value=value, updated_at=now))
        else:
            row.value = value
            row.updated_at = now
        return PairingCode(code=code, expires_at=expires_at)

    async def redeem(self, session: AsyncSession, code: str) -> None:
        """Use up `code`, or raise PairingError if it is wrong, expired or guessed at."""
        now = self._clock.now()
        self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW]
        if len(self._failures) >= MAX_FAILURES:
            raise PairingError("Too many attempts. Wait 15 minutes.", 429)
        row = await session.get(SystemState, self._key, with_for_update=True)
        valid = (
            row is not None
            and secrets.compare_digest(str(row.value.get("hash", "")), sha256_hex(canonical(code)))
            and datetime.fromisoformat(str(row.value.get("expires_at"))) >= now
        )
        if not valid or row is None:
            self._failures.append(now)
            raise PairingError("That pairing code is wrong or has expired.", 403)
        await session.delete(row)  # single use
