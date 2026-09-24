"""Pairing voice satellites (like the Windows tray app) without passwords.

You make a one-time pairing code in the app (with a passkey tap); the satellite
trades it for a device token. The token only opens voice sessions, is stored
hashed, and can be revoked in Settings at any time.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.log import AuditLog
from jarvis.clock import Clock
from jarvis.db.models import VoiceDevice
from jarvis.security.hashing import sha256_hex
from jarvis.security.pairing import PairingCode, PairingCodes

PAIRING_KEY = "voice_pairing"
TOKEN_PREFIX = "jv_"  # noqa: S105 (a public prefix, not a secret)
_SEEN_EVERY = timedelta(minutes=5)


class DeviceService:
    def __init__(self, *, clock: Clock, audit: AuditLog) -> None:
        self._clock = clock
        self._audit = audit
        self._codes = PairingCodes(PAIRING_KEY, clock=clock)

    async def create_pairing_code(self, session: AsyncSession) -> PairingCode:
        code = await self._codes.issue(session)
        await self._audit.append(
            session,
            actor="owner",
            event_type="voice.pairing_code",
            summary="Made a voice-satellite pairing code",
        )
        return code

    async def pair(self, session: AsyncSession, code: str, name: str) -> tuple[VoiceDevice, str]:
        """Trade a pairing code for a device token. Raises PairingError."""
        await self._codes.redeem(session, code)
        now = self._clock.now()
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        device = VoiceDevice(
            id=uuid.uuid4(),
            name=(name.strip() or "Voice satellite")[:80],
            token_hash=sha256_hex(token),
            created_at=now,
        )
        session.add(device)
        await self._audit.append(
            session,
            actor="owner",
            event_type="voice.device_paired",
            subject_type="voice_device",
            subject_id=str(device.id),
            summary=f"Paired voice satellite '{device.name}'",
        )
        return device, token

    async def authenticate(self, session: AsyncSession, token: str | None) -> VoiceDevice | None:
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        device = await session.scalar(
            select(VoiceDevice)
            .where(VoiceDevice.token_hash == sha256_hex(token))
            .where(VoiceDevice.revoked_at.is_(None))
        )
        if device is not None:
            now = self._clock.now()
            if device.last_seen_at is None or now - device.last_seen_at >= _SEEN_EVERY:
                device.last_seen_at = now
        return device

    async def list_devices(self, session: AsyncSession) -> list[VoiceDevice]:
        rows = await session.scalars(
            select(VoiceDevice)
            .where(VoiceDevice.revoked_at.is_(None))
            .order_by(VoiceDevice.created_at)
        )
        return list(rows)

    async def revoke(self, session: AsyncSession, device_id: uuid.UUID) -> bool:
        device = await session.get(VoiceDevice, device_id, with_for_update=True)
        if device is None or device.revoked_at is not None:
            return False
        device.revoked_at = self._clock.now()
        await self._audit.append(
            session,
            actor="owner",
            event_type="voice.device_revoked",
            subject_type="voice_device",
            subject_id=str(device.id),
            summary=f"Removed voice satellite '{device.name}'",
        )
        return True
