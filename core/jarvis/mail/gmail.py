"""A small Gmail (and Calendar) REST client over httpx.

Reads are retried with backoff when Gmail is busy (429, 5xx). Writes that
could land twice (sending, creating drafts) are never retried here: if the
answer is lost after a send, the caller gets `SendOutcomeUnknown` and Jarvis
checks Sent mail instead of guessing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from jarvis.ingestion.google import GoogleEndpoints, GoogleError

TokenSource = Callable[[], Awaitable[str]]
READ_RETRIES = 3
HISTORY_TYPES = ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")


class GmailError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GmailAuthError(GmailError):
    """Access was revoked, or a needed scope wasn't granted: you must reconnect."""


class HistoryExpired(GmailError):
    """The sync point is too old (Gmail answered 404): do a full sync."""


class SendOutcomeUnknown(GmailError):
    """The request reached Gmail but its answer didn't come back."""


class SendRefused(GmailError):
    """Gmail refused the message, or never got it: nothing was sent."""


def _reason(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return response.text[:200]
    message = error.get("message") or ""
    reasons = {e.get("reason") for e in error.get("errors") or [] if isinstance(e, dict)}
    status = error.get("status") or ""
    return " ".join(str(x) for x in (message, status, *sorted(r for r in reasons if r)) if x)


def _is_rate_limit(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    return response.status_code == 403 and "ratelimitexceeded" in _reason(response).lower()


class GmailClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: TokenSource,
        *,
        endpoints: GoogleEndpoints | None = None,
        backoff: float = 1.0,
    ) -> None:
        self._http = http
        self._token = token
        self._endpoints = endpoints or GoogleEndpoints()
        self._backoff = backoff

    @property
    def base(self) -> str:
        return self._endpoints.gmail

    async def _headers(self) -> dict[str, str]:
        try:
            token = await self._token()
        except GoogleError as exc:
            raise GmailAuthError(str(exc)) from exc
        return {"Authorization": f"Bearer {token}"}

    def _check(self, response: httpx.Response, what: str) -> None:
        if response.status_code < 400:
            return
        reason = _reason(response)
        if response.status_code == 401 or (
            response.status_code == 403
            and ("insufficient" in reason.lower() or "scope" in reason.lower())
        ):
            raise GmailAuthError(
                f"Google refused {what}: {reason or response.status_code}. "
                "Reconnect Google in Jarvis (Sources).",
                status=response.status_code,
            )
        raise GmailError(
            f"{what} failed ({response.status_code}): {reason}", status=response.status_code
        )

    async def _read(self, method: str, url: str, what: str, **kwargs: Any) -> dict[str, Any]:
        """An idempotent request: retried with backoff when Gmail is busy."""
        for attempt in range(READ_RETRIES + 1):
            response = await self._http.request(
                method, url, headers=await self._headers(), **kwargs
            )
            if (_is_rate_limit(response) or response.status_code >= 500) and attempt < READ_RETRIES:
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            self._check(response, what)
            return response.json() if response.content else {}
        raise AssertionError("unreachable")

    # --- Reading ----------------------------------------------------------------

    async def profile(self) -> dict[str, Any]:
        return await self._read("GET", f"{self.base}/profile", "Reading your Gmail profile")

    async def list_messages(
        self, *, query: str, page_token: str | None = None, max_results: int = 100
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return await self._read("GET", f"{self.base}/messages", "Listing messages", params=params)

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        """A message (format=full), or None if it no longer exists."""
        try:
            return await self._read(
                "GET",
                f"{self.base}/messages/{message_id}",
                "Reading a message",
                params={"format": "full"},
            )
        except GmailError as exc:
            if exc.status == 404:
                return None
            raise

    async def history(self, start_history_id: int, page_token: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "startHistoryId": str(start_history_id),
            "historyTypes": list(HISTORY_TYPES),
            "maxResults": 500,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            return await self._read(
                "GET", f"{self.base}/history", "Checking for changes", params=params
            )
        except GmailError as exc:
            if exc.status == 404:
                raise HistoryExpired("The sync point expired", status=404) from exc
            raise

    async def labels(self) -> list[dict[str, Any]]:
        data = await self._read("GET", f"{self.base}/labels", "Listing labels")
        return list(data.get("labels") or [])

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        try:
            return await self._read(
                "GET",
                f"{self.base}/drafts/{draft_id}",
                "Reading a draft",
                params={"format": "full"},
            )
        except GmailError as exc:
            if exc.status == 404:
                return None
            raise

    # --- Writing ---------------------------------------------------------------------

    async def _write(self, method: str, url: str, what: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._http.request(method, url, headers=await self._headers(), **kwargs)
        self._check(response, what)
        return response.json() if response.content else {}

    async def create_label(self, name: str) -> dict[str, Any]:
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        return await self._write("POST", f"{self.base}/labels", "Creating a label", json=body)

    async def modify_message(
        self, message_id: str, *, add: list[str], remove: list[str]
    ) -> dict[str, Any]:
        body = {"addLabelIds": add, "removeLabelIds": remove}
        return await self._write(
            "POST", f"{self.base}/messages/{message_id}/modify", "Labelling a message", json=body
        )

    async def create_draft(self, raw: str, thread_id: str | None) -> dict[str, Any]:
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        return await self._write(
            "POST", f"{self.base}/drafts", "Saving a draft", json={"message": message}
        )

    async def update_draft(self, draft_id: str, raw: str, thread_id: str | None) -> dict[str, Any]:
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        return await self._write(
            "PUT",
            f"{self.base}/drafts/{draft_id}",
            "Updating a draft",
            json={"id": draft_id, "message": message},
        )

    async def delete_draft(self, draft_id: str) -> None:
        response = await self._http.delete(
            f"{self.base}/drafts/{draft_id}", headers=await self._headers()
        )
        if response.status_code != 404:
            self._check(response, "Deleting a draft")

    async def send(self, raw: str, thread_id: str | None) -> dict[str, Any]:
        """Send exactly `raw`. Never retried: see SendOutcomeUnknown."""
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        headers = await self._headers()
        try:
            response = await self._http.post(
                f"{self.base}/messages/send", headers=headers, json=body
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise SendRefused(
                f"Couldn't reach Gmail ({type(exc).__name__}); nothing was sent"
            ) from exc
        except httpx.HTTPError as exc:  # sent, but the answer was lost
            raise SendOutcomeUnknown(
                f"No answer from Gmail after sending ({type(exc).__name__})"
            ) from exc
        if response.status_code >= 500:
            raise SendOutcomeUnknown(f"Gmail had an error after the send ({response.status_code})")
        if response.status_code >= 400:
            try:
                self._check(response, "Sending")
            except GmailAuthError:
                raise
            except GmailError as exc:
                raise SendRefused(str(exc), status=exc.status) from exc
        try:
            return dict(response.json())
        except ValueError as exc:  # accepted, so it went out: never report it as failed
            raise SendOutcomeUnknown(
                "Gmail accepted the email but its answer was unreadable"
            ) from exc

    async def insert_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return await self._write(
            "POST",
            f"{self._endpoints.calendar}/calendars/primary/events",
            "Adding a calendar hold",
            json=event,
        )
