"""Web Push notifications to the owner's own devices (VAPID, no third party)."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select

from jarvis.config import Settings
from jarvis.db.models import PushSubscription
from jarvis.db.session import SessionFactory, transaction


@dataclass(frozen=True)
class PushResult:
    delivered: int
    removed: int
    configured: bool


class PushService:
    def __init__(self, *, settings: Settings, session_factory: SessionFactory) -> None:
        self._settings = settings
        self._sf = session_factory

    @property
    def configured(self) -> bool:
        return bool(self._settings.vapid_public_key and self._settings.vapid_private_key)

    async def subscribe(
        self, *, endpoint: str, keys: dict[str, str], user_agent: str | None, now: Any
    ) -> uuid.UUID:
        async with transaction(self._sf) as session:
            existing = await session.scalar(
                select(PushSubscription).where(PushSubscription.endpoint == endpoint)
            )
            if existing is not None:
                existing.keys = keys
                return existing.id
            sub = PushSubscription(
                id=uuid.uuid4(), endpoint=endpoint, keys=keys, user_agent=user_agent, created_at=now
            )
            session.add(sub)
            return sub.id

    async def send(self, *, title: str, body: str, url: str | None = None) -> PushResult:
        if not self.configured:
            return PushResult(delivered=0, removed=0, configured=False)
        from pywebpush import WebPushException, webpush

        async with self._sf() as session:
            subs = list(await session.scalars(select(PushSubscription)))
        payload = json.dumps({"title": title, "body": body, "url": url or "/"})
        private_key = self._settings.vapid_private_key
        assert private_key is not None
        delivered, gone = 0, []
        for sub in subs:
            try:
                await asyncio.to_thread(
                    webpush,
                    subscription_info={"endpoint": sub.endpoint, "keys": sub.keys},
                    data=payload,
                    vapid_private_key=private_key.get_secret_value(),
                    vapid_claims={"sub": self._settings.vapid_subject},
                    ttl=3600,
                )
                delivered += 1
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):  # subscription expired: clean it up
                    gone.append(sub.id)
        if gone:
            async with transaction(self._sf) as session:
                await session.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
        return PushResult(delivered=delivered, removed=len(gone), configured=True)
