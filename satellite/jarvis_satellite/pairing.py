"""Pairing this PC with Jarvis, and keeping its device token safe.

You make a one-time code in Jarvis (Settings → Voice, with a passkey tap). This
trades it for a device token that only opens voice sessions. The token goes to
Windows Credential Manager (via keyring) and is never printed or logged.
"""

from __future__ import annotations

import contextlib
from typing import Protocol

import httpx

from jarvis_satellite.config import Settings

SERVICE = "jarvis-satellite"
TOKEN_PREFIX = "jv_"  # noqa: S105 (a public prefix, not a secret)


class PairingError(Exception):
    pass


class TokenStore(Protocol):
    def get(self, server: str) -> str | None: ...
    def set(self, server: str, token: str) -> None: ...
    def delete(self, server: str) -> None: ...


class KeyringStore:
    """Windows Credential Manager (or the system keyring elsewhere)."""

    def get(self, server: str) -> str | None:
        import keyring

        return keyring.get_password(SERVICE, server)

    def set(self, server: str, token: str) -> None:
        import keyring

        keyring.set_password(SERVICE, server, token)

    def delete(self, server: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        with contextlib.suppress(PasswordDeleteError):  # already gone
            keyring.delete_password(SERVICE, server)


class MemoryStore:
    """Keeps the token in memory only (tests)."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    def get(self, server: str) -> str | None:
        return self.tokens.get(server)

    def set(self, server: str, token: str) -> None:
        self.tokens[server] = token

    def delete(self, server: str) -> None:
        self.tokens.pop(server, None)


def pair(
    settings: Settings,
    code: str,
    *,
    store: TokenStore,
    client: httpx.Client | None = None,
) -> None:
    """Trade a pairing code for this PC's device token and store it."""
    url = f"{settings.server}/api/voice/devices/pair"
    http = client or httpx.Client(timeout=15)
    try:
        response = http.post(url, json={"code": code.strip(), "name": settings.name})
    except httpx.HTTPError as exc:
        raise PairingError(
            f"Can't reach Jarvis at {settings.server} ({type(exc).__name__}). "
            "Is it running? On the PC: make up"
        ) from None
    finally:
        if client is None:
            http.close()
    if response.status_code == 403:
        raise PairingError(
            "That code is wrong or has expired. Make a new one in Jarvis: "
            "Settings → Voice → Pair the Windows app."
        )
    if response.status_code == 429:
        raise PairingError("Too many wrong codes. Wait 15 minutes, then use a new code.")
    if response.status_code != 200:
        raise PairingError(f"Jarvis refused the pairing (HTTP {response.status_code}).")
    try:
        token = str(response.json()["token"])
    except (ValueError, KeyError, TypeError):
        raise PairingError("Jarvis sent an unexpected reply to the pairing.") from None
    if not token.startswith(TOKEN_PREFIX):
        raise PairingError("Jarvis sent an unexpected reply to the pairing.")
    store.set(settings.server, token)
