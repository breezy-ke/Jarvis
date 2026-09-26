"""Google OAuth (installed-app flow with PKCE), plus Gmail and Calendar reads.

Connecting asks for Gmail and Calendar access (`EMAIL_SCOPES`). Google lets
you untick any of them, so Jarvis works with whatever you grant: with read-only
Gmail it sorts and summarises; with `gmail.modify` it can also label, draft and
send, and even then only through the policy engine, with your approval.
Tokens are stored encrypted with JARVIS_SECRET_KEY.

The OAuth redirect is a loopback address (http://127.0.0.1:8080/...), as
Google requires for Desktop clients, so connect Google from a browser on
the PC running Jarvis. The `state` value (random, single-use, 10 minutes)
protects the callback.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.clock import Clock
from jarvis.config import Settings
from jarvis.db.models import OAuthPending, OAuthToken
from jarvis.security.crypto import Vault

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a secret)
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_READONLY = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
READONLY_SCOPES = (GMAIL_READONLY, CALENDAR_READONLY)
# Phase 3: read, label, draft and send mail (never delete), and add calendar holds.
EMAIL_SCOPES = (GMAIL_MODIFY, CALENDAR_READONLY, CALENDAR_EVENTS)
PROVIDER = "google"


@dataclass(frozen=True)
class GoogleEndpoints:
    """Where Google lives. Tests point these at a fake server (JARVIS_GOOGLE_FAKE_BASE)."""

    auth: str = AUTH_URL
    token: str = TOKEN_URL
    revoke: str = REVOKE_URL
    gmail: str = GMAIL
    calendar: str = CALENDAR

    @classmethod
    def fake(cls, base: str) -> GoogleEndpoints:
        base = base.rstrip("/")
        return cls(
            auth=f"{base}/o/oauth2/v2/auth",
            token=f"{base}/token",
            revoke=f"{base}/revoke",
            gmail=f"{base}/gmail/v1/users/me",
            calendar=f"{base}/calendar/v3",
        )


class GoogleError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleConnection:
    connected: bool
    account_email: str | None = None
    scopes: str | None = None

    def granted(self, scope: str) -> bool:
        return self.connected and scope in (self.scopes or "").split()

    @property
    def mail_access(self) -> str:
        """ "full" (read, label, draft, send), "read", or "none"."""
        if self.granted(GMAIL_MODIFY):
            return "full"
        if self.granted(GMAIL_READONLY):
            return "read"
        return "none"

    @property
    def can_add_holds(self) -> bool:
        return self.granted(CALENDAR_EVENTS)


class GoogleAuth:
    @classmethod
    def from_settings(
        cls, settings: Settings, *, vault: Vault, clock: Clock, http: httpx.AsyncClient
    ) -> GoogleAuth:
        """The Google connection `.env` describes (a fake Google in tests)."""
        return cls(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret.get_secret_value()
            if settings.google_client_secret
            else None,
            redirect_uri=settings.google_redirect_uri,
            vault=vault,
            clock=clock,
            http=http,
            endpoints=GoogleEndpoints.fake(settings.google_fake_base)
            if settings.google_fake_base
            else None,
        )

    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str,
        vault: Vault,
        clock: Clock,
        http: httpx.AsyncClient,
        endpoints: GoogleEndpoints | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._vault = vault
        self._clock = clock
        self._http = http
        self.endpoints = endpoints or GoogleEndpoints()

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _require(self) -> tuple[str, str]:
        if not (self._client_id and self._client_secret):
            raise GoogleError(
                "Google isn't set up: add GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET."
            )
        return self._client_id, self._client_secret

    async def start(self, session: AsyncSession, *, scopes: tuple[str, ...] = EMAIL_SCOPES) -> str:
        client_id, _ = self._require()
        now = self._clock.now()
        await session.execute(delete(OAuthPending).where(OAuthPending.expires_at < now))
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"="
        )
        session.add(
            OAuthPending(
                state=state,
                provider=PROVIDER,
                code_verifier_encrypted=self._vault.encrypt(verifier),
                redirect_uri=self.redirect_uri,
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            )
        )
        params = {
            "client_id": client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
        }
        return f"{self.endpoints.auth}?{urlencode(params)}"

    async def finish(self, session: AsyncSession, *, state: str, code: str) -> GoogleConnection:
        client_id, client_secret = self._require()
        pending = await session.get(OAuthPending, state, with_for_update=True)
        if pending is None or pending.provider != PROVIDER:
            raise GoogleError("This sign-in link is invalid or was already used. Start again.")
        await session.delete(pending)  # single use, even if the exchange fails
        if pending.expires_at < self._clock.now():
            raise GoogleError("This sign-in link expired. Start again.")
        verifier = self._vault.decrypt_str(pending.code_verifier_encrypted)
        resp = await self._http.post(
            self.endpoints.token,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": pending.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code != 200:
            raise GoogleError(f"Google refused the sign-in ({resp.status_code}).")
        token = resp.json()
        if "refresh_token" not in token:
            raise GoogleError(
                "Google didn't return a refresh token. Remove Jarvis's access and retry."
            )
        token["expires_at"] = (
            self._clock.now() + timedelta(seconds=int(token.get("expires_in", 3600)))
        ).isoformat()
        email = await self._account_email(token["access_token"])
        await self._store(session, token, email)
        return GoogleConnection(True, email, token.get("scope"))

    async def _account_email(self, access_token: str) -> str | None:
        resp = await self._http.get(
            f"{self.endpoints.gmail}/profile", headers={"Authorization": f"Bearer {access_token}"}
        )
        return resp.json().get("emailAddress") if resp.status_code == 200 else None

    async def _store(self, session: AsyncSession, token: dict[str, Any], email: str | None) -> None:
        row = await session.get(OAuthToken, PROVIDER, with_for_update=True)
        blob = self._vault.encrypt(json.dumps(token))
        if row is None:
            session.add(
                OAuthToken(
                    provider=PROVIDER,
                    token_encrypted=blob,
                    scopes=token.get("scope", ""),
                    account_email=email,
                    updated_at=self._clock.now(),
                )
            )
        else:
            row.token_encrypted = blob
            row.scopes = token.get("scope", row.scopes)
            row.account_email = email or row.account_email
            row.updated_at = self._clock.now()

    async def connection(self, session: AsyncSession) -> GoogleConnection:
        row = await session.get(OAuthToken, PROVIDER)
        if row is None:
            return GoogleConnection(False)
        return GoogleConnection(True, row.account_email, row.scopes)

    async def access_token(self, session: AsyncSession) -> str:
        client_id, client_secret = self._require()
        row = await session.get(OAuthToken, PROVIDER, with_for_update=True)
        if row is None:
            raise GoogleError("Google isn't connected yet.")
        token = json.loads(self._vault.decrypt_str(row.token_encrypted))
        expires_at = datetime.fromisoformat(token["expires_at"])
        if expires_at - timedelta(minutes=2) > self._clock.now():
            return str(token["access_token"])
        resp = await self._http.post(
            self.endpoints.token,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": token["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            raise GoogleError(
                "Google access expired or was revoked. Reconnect Google in Settings "
                "(if the consent screen is still in 'Testing' mode, switch it to 'In production')."
            )
        fresh = resp.json()
        token["access_token"] = fresh["access_token"]
        token["expires_at"] = (
            self._clock.now() + timedelta(seconds=int(fresh.get("expires_in", 3600)))
        ).isoformat()
        await self._store(session, token, row.account_email)
        return str(token["access_token"])

    async def disconnect(self, session: AsyncSession) -> None:
        row = await session.get(OAuthToken, PROVIDER, with_for_update=True)
        if row is None:
            return
        token = json.loads(self._vault.decrypt_str(row.token_encrypted))
        # Revoking is best effort: the local token is deleted regardless.
        with contextlib.suppress(httpx.HTTPError):
            await self._http.post(
                self.endpoints.revoke, params={"token": token.get("refresh_token", "")}
            )
        await session.delete(row)


def _decode_part(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def message_text(payload: dict[str, Any]) -> str:
    """Pull the plain-text body out of a Gmail API message payload."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    if mime == "text/plain" and body.get("data"):
        return _decode_part(body["data"])
    for part in payload.get("parts", []) or []:
        text = message_text(part)
        if text:
            return text
    if mime == "text/html" and body.get("data"):
        import trafilatura

        return trafilatura.extract(_decode_part(body["data"])) or ""
    return ""


async def fetch_sent_bodies(
    http: httpx.AsyncClient, access_token: str, *, limit: int = 200, gmail: str = GMAIL
) -> list[str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    ids: list[str] = []
    page: str | None = None
    while len(ids) < limit:
        params: dict[str, str | int] = {
            "labelIds": "SENT",
            "maxResults": min(100, limit - len(ids)),
        }
        if page:
            params["pageToken"] = page
        resp = await http.get(f"{gmail}/messages", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        ids.extend(m["id"] for m in data.get("messages", []))
        page = data.get("nextPageToken")
        if not page:
            break
    bodies: list[str] = []
    for message_id in ids[:limit]:
        resp = await http.get(
            f"{gmail}/messages/{message_id}", headers=headers, params={"format": "full"}
        )
        if resp.status_code == 200:
            text = message_text(resp.json().get("payload", {}))
            if text:
                bodies.append(text)
    return bodies


async def fetch_calendar_events(
    http: httpx.AsyncClient,
    access_token: str,
    *,
    start: datetime,
    end: datetime,
    calendar: str = CALENDAR,
) -> list[dict[str, Any]]:
    resp = await http.get(
        f"{calendar}/calendars/primary/events",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        },
    )
    resp.raise_for_status()
    return list(resp.json().get("items", []))


def calendar_suggestions(events: list[dict[str, Any]], tz: Any) -> dict[str, object]:
    """Derive working hours and routines from event timing (titles only for recurring events)."""
    starts: list[datetime] = []
    ends: list[datetime] = []
    recurring: Counter[str] = Counter()
    for event in events:
        start = (event.get("start") or {}).get("dateTime")
        end = (event.get("end") or {}).get("dateTime")
        if not (start and end):
            continue  # all-day events say nothing about hours
        starts.append(datetime.fromisoformat(start).astimezone(tz))
        ends.append(datetime.fromisoformat(end).astimezone(tz))
        if event.get("recurringEventId") and event.get("summary"):
            recurring[event["summary"].strip()[:60]] += 1
    if len(starts) < 5:
        return {}
    weekdays = sorted({s.weekday() for s in starts})
    first = sorted(s.hour for s in starts)[len(starts) // 10]
    last = sorted(e.hour + (1 if e.minute else 0) for e in ends)[-(len(ends) // 10) - 1]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days = (
        f"{names[weekdays[0]]}-{names[weekdays[-1]]}" if len(weekdays) > 1 else names[weekdays[0]]
    )
    suggestions: dict[str, object] = {
        "schedule.working_hours": f"{days} {first:02d}:00-{last:02d}:00"
    }
    routines = [name for name, count in recurring.most_common(5) if count >= 3]
    if routines:
        suggestions["schedule.routines"] = routines
    return suggestions
