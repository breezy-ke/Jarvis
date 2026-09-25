"""Telling you something directly: a push notification, and Telegram when it's linked.

For alerts and digests. These go only to you, and they aren't actions on anyone
else, so they don't pass through the policy engine. Anything secret-looking is
masked on Telegram, which isn't end-to-end encrypted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from jarvis.services import Services

log = logging.getLogger("jarvis.notify")


class OwnerChannel(Protocol):
    async def tell_owner(self, text: str, *, silent: bool = False) -> bool: ...


@dataclass(frozen=True)
class Delivered:
    push: int  # devices reached
    telegram: bool

    @property
    def anywhere(self) -> bool:
        return self.push > 0 or self.telegram


class OwnerNotifier:
    def __init__(self, services: Services) -> None:
        self._s = services
        self.telegram: OwnerChannel | None = None  # attached once the bot is running

    def quiet_now(self) -> bool:
        return self._s.policies_config.quiet_at(self._s.clock.now())

    async def tell(
        self,
        *,
        title: str,
        body: str,
        url: str | None = None,
        detail: str | None = None,
        silent: bool = False,
    ) -> Delivered:
        """Push `title` and `body`; Telegram gets `detail` (or the body) under the title.

        A silent note skips the push (phones always sound for those) and arrives
        on Telegram without a sound.
        """
        pushed = 0
        if not silent:
            try:
                pushed = (await self._s.push.send(title=title, body=body, url=url)).delivered
            except Exception:
                log.warning("couldn't send a push notification", exc_info=True)
        sent = False
        if self.telegram is not None:
            try:
                sent = await self.telegram.tell_owner(f"{title}\n\n{detail or body}", silent=silent)
            except Exception:
                log.warning("couldn't send a Telegram note", exc_info=True)
        return Delivered(pushed, sent)
