"""A fake Google (sign-in, Gmail and Calendar) with an in-memory mailbox.

It speaks the parts of the real APIs Jarvis uses, with real history ids, labels,
drafts and Sent mail, so the sync and send code runs unchanged. Tests use it in
process (httpx.ASGITransport); the browser tests serve it over HTTP
(e2e_server.py). Faults can be switched on to test the unhappy paths.
"""

from __future__ import annotations

import base64
import itertools
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
SYSTEM_LABELS = ("INBOX", "UNREAD", "SENT", "DRAFT", "SPAM", "TRASH", "IMPORTANT", "STARRED")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _payload(msg: Message) -> dict[str, Any]:
    """An email as Gmail's `payload` tree."""
    headers = [{"name": k, "value": str(v)} for k, v in msg.items()]
    part: dict[str, Any] = {
        "mimeType": msg.get_content_type(),
        "filename": msg.get_filename() or "",
        "headers": headers,
        "body": {"size": 0},
    }
    if msg.is_multipart():
        part["parts"] = [_payload(p) for p in msg.get_payload()]  # type: ignore[union-attr]
    else:
        data = msg.get_payload(decode=True) or b""
        assert isinstance(data, bytes)
        part["body"] = {"size": len(data), "data": _b64(data)}
    return part


class FakeGmail:
    def __init__(
        self,
        *,
        account: str = "owner@example.com",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.account = account
        self.now = now or (lambda: datetime.now(UTC))
        self.history_id = 1_000
        self.oldest_history = 0
        self.history: list[dict[str, Any]] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.labels: dict[str, str] = {name: name for name in SYSTEM_LABELS}  # id -> name
        self.events: list[dict[str, Any]] = []
        self.granted = f"{GMAIL_MODIFY} {CALENDAR_EVENTS}"
        self.faults: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        self._ids = itertools.count(1)
        self.app = self._build_app()

    # --- Test helpers ------------------------------------------------------------------

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):06x}"

    def _record(self, kind: str, message: dict[str, Any], **extra: Any) -> None:
        self.history_id += 1
        entry = {
            "id": message["id"],
            "threadId": message["threadId"],
            "labelIds": list(message["labelIds"]),
        }
        self.history.append({"id": str(self.history_id), kind: [{"message": entry, **extra}]})
        message["historyId"] = str(self.history_id)

    def _store(
        self, raw: bytes, *, thread_id: str | None, labels: Iterable[str], when: datetime
    ) -> dict[str, Any]:
        message_id = self._next_id("m")
        message = {
            "id": message_id,
            "threadId": thread_id or message_id,
            "labelIds": list(labels),
            "raw": raw,
            "internalDate": str(int(when.timestamp() * 1000)),
            "historyId": str(self.history_id),
        }
        self.messages[message_id] = message
        self._record("messagesAdded", message)
        return message

    def deliver(
        self,
        *,
        sender: str = "Achieng Otieno <achieng@client.co.ke>",
        to: str | None = None,
        cc: str | None = None,
        subject: str = "Kickoff next week",
        body: str = "Hi, can we meet on Tuesday at 10:00 to kick off the project?",
        html: str | None = None,
        thread_id: str | None = None,
        reply_to_message: str | None = None,
        headers: dict[str, str] | None = None,
        labels: Iterable[str] = ("INBOX", "UNREAD"),
        when: datetime | None = None,
        attachments: Iterable[str] = (),
    ) -> str:
        """An email arrives. Returns its Gmail message id."""
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to or self.account
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg["Message-ID"] = f"<{self._next_id('x')}@mail.test>"
        if reply_to_message is not None:
            parent = message_from_bytes(self.messages[reply_to_message]["raw"])
            msg["In-Reply-To"] = parent["Message-ID"]
            msg["References"] = parent["Message-ID"]
            thread_id = thread_id or self.messages[reply_to_message]["threadId"]
        for name, value in (headers or {}).items():
            msg[name] = value
        msg.set_content(body)
        if html is not None:
            msg.add_alternative(html, subtype="html")
        for name in attachments:
            msg.add_attachment(
                b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename=name
            )
        stored = self._store(
            msg.as_bytes(policy=policy.SMTP),
            thread_id=thread_id,
            labels=labels,
            when=when or self.now(),
        )
        return str(stored["id"])

    def owner_sends(self, *, thread_id: str, to: str, subject: str, body: str) -> str:
        """You reply from Gmail itself (not through Jarvis)."""
        msg = EmailMessage()
        msg["From"] = self.account
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = f"<{self._next_id('x')}@mail.test>"
        msg.set_content(body)
        stored = self._store(
            msg.as_bytes(policy=policy.SMTP), thread_id=thread_id, labels=["SENT"], when=self.now()
        )
        return str(stored["id"])

    def relabel(
        self, message_id: str, *, add: Iterable[str] = (), remove: Iterable[str] = ()
    ) -> None:
        message = self.messages[message_id]
        message["labelIds"] = [x for x in message["labelIds"] if x not in set(remove)]
        message["labelIds"] += [x for x in add if x not in message["labelIds"]]
        kind = "labelsAdded" if add else "labelsRemoved"
        self._record(kind, message, labelIds=list(add or remove))

    def owner_edits_draft(self, draft_id: str, body: str) -> None:
        """You change a draft in Gmail itself."""
        message = self.messages[self.drafts[draft_id]["message_id"]]
        parsed = message_from_bytes(message["raw"], policy=policy.default)
        assert isinstance(parsed, EmailMessage)
        parsed.set_content(body)
        message["raw"] = parsed.as_bytes(policy=policy.SMTP)

    def delete(self, message_id: str) -> None:
        message = self.messages.pop(message_id)
        self._record("messagesDeleted", message)

    def expire_history(self) -> None:
        """As if Jarvis had been offline for weeks: old sync points stop working."""
        self.oldest_history = self.history_id

    def sent(self) -> list[Message]:
        """What was sent through the API, as parsed emails."""
        return [
            message_from_bytes(m["raw"], policy=policy.default)
            for m in self.messages.values()
            if "SENT" in m["labelIds"] and m.get("via_api")
        ]

    def resource(self, message: dict[str, Any], fmt: str = "full") -> dict[str, Any]:
        parsed = message_from_bytes(message["raw"])
        resource: dict[str, Any] = {
            "id": message["id"],
            "threadId": message["threadId"],
            "labelIds": list(message["labelIds"]),
            "historyId": message["historyId"],
            "internalDate": message["internalDate"],
            "sizeEstimate": len(message["raw"]),
            "snippet": " ".join(str(parsed.get("Subject", "")).split())[:100],
        }
        if fmt == "raw":
            resource["raw"] = _b64(message["raw"])
        else:
            resource["payload"] = _payload(parsed)
        return resource

    # --- The fake HTTP API ---------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI()
        fake = self

        @app.middleware("http")
        async def log_requests(request: Request, call_next: Callable[[Request], Any]) -> Any:
            fake.requests.append((request.method, request.url.path))
            return await call_next(request)

        def authorized(request: Request, scope: str | None = None) -> JSONResponse | None:
            if (
                "revoked" in fake.faults
                or request.headers.get("authorization") != "Bearer fake-access"
            ):
                return JSONResponse({"error": {"code": 401, "message": "Invalid Credentials"}}, 401)
            if scope and scope not in fake.granted.split():
                return JSONResponse(
                    {
                        "error": {
                            "code": 403,
                            "message": "Request had insufficient authentication scopes.",
                            "status": "PERMISSION_DENIED",
                            "errors": [{"reason": "insufficientPermissions"}],
                        }
                    },
                    403,
                )
            if "rate_limit_once" in fake.faults:
                fake.faults.discard("rate_limit_once")
                return JSONResponse({"error": {"code": 429, "message": "Rate limit"}}, 429)
            return None

        # Sign-in
        @app.get("/o/oauth2/v2/auth")
        async def authorize(redirect_uri: str, state: str, scope: str) -> RedirectResponse:
            if "untick_modify" in fake.faults:
                scope = (
                    " ".join(s for s in scope.split() if s != GMAIL_MODIFY) + f" {GMAIL_READONLY}"
                )
            fake.granted = scope
            query = urlencode({"code": "fake-code", "state": state, "scope": scope})
            return RedirectResponse(f"{redirect_uri}?{query}", status_code=302)

        @app.post("/token")
        async def token(request: Request) -> JSONResponse:
            form = await request.form()
            if "revoked" in fake.faults:
                return JSONResponse({"error": "invalid_grant"}, 400)
            body: dict[str, Any] = {
                "access_token": "fake-access",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
            if form.get("grant_type") == "authorization_code":
                body |= {"refresh_token": "fake-refresh", "scope": fake.granted}
            return JSONResponse(body)

        @app.post("/revoke")
        async def revoke() -> Response:
            return Response(status_code=200)

        # Gmail
        base = "/gmail/v1/users/me"

        @app.get(f"{base}/profile")
        async def profile(request: Request) -> JSONResponse:
            if (denied := authorized(request)) is not None:
                return denied
            return JSONResponse(
                {
                    "emailAddress": fake.account,
                    "historyId": str(fake.history_id),
                    "messagesTotal": len(fake.messages),
                }
            )

        @app.get(f"{base}/messages")
        async def list_messages(
            request: Request, q: str = "", maxResults: int = 100, pageToken: str | None = None
        ) -> JSONResponse:
            if (denied := authorized(request)) is not None:
                return denied
            items = sorted(
                fake.messages.values(), key=lambda m: int(m["internalDate"]), reverse=True
            )
            if match := re.search(r"newer_than:(\d+)d", q):
                cutoff = fake.now() - timedelta(days=int(match.group(1)))
                items = [m for m in items if int(m["internalDate"]) >= cutoff.timestamp() * 1000]
            if "in:sent" in q:
                items = [m for m in items if "SENT" in m["labelIds"]]
            items = [m for m in items if not {"SPAM", "TRASH", "DRAFT"} & set(m["labelIds"])]
            start = int(pageToken or 0)
            page = items[start : start + maxResults]
            body: dict[str, Any] = {
                "messages": [{"id": m["id"], "threadId": m["threadId"]} for m in page]
            }
            if start + maxResults < len(items):
                body["nextPageToken"] = str(start + maxResults)
            return JSONResponse(body)

        @app.get(f"{base}/messages/{{message_id}}")
        async def get_message(
            request: Request, message_id: str, format: str = "full"
        ) -> JSONResponse:
            if (denied := authorized(request)) is not None:
                return denied
            message = fake.messages.get(message_id)
            if message is None:
                return JSONResponse({"error": {"code": 404, "message": "Not Found"}}, 404)
            return JSONResponse(fake.resource(message, format))

        @app.get(f"{base}/history")
        async def history(
            request: Request, startHistoryId: str, pageToken: str | None = None
        ) -> JSONResponse:
            if (denied := authorized(request)) is not None:
                return denied
            start = int(startHistoryId)
            if start < fake.oldest_history or "history_404" in fake.faults:
                fake.faults.discard("history_404")
                return JSONResponse(
                    {"error": {"code": 404, "message": "Requested entity was not found."}}, 404
                )
            records = [r for r in fake.history if int(r["id"]) > start]
            offset = int(pageToken or 0)
            page = records[offset : offset + 2]  # small pages, so paging is exercised
            body: dict[str, Any] = {"history": page, "historyId": str(fake.history_id)}
            if offset + 2 < len(records):
                body["nextPageToken"] = str(offset + 2)
            return JSONResponse(body)

        @app.get(f"{base}/labels")
        async def labels(request: Request) -> JSONResponse:
            if (denied := authorized(request)) is not None:
                return denied
            return JSONResponse({"labels": [{"id": i, "name": n} for i, n in fake.labels.items()]})

        @app.post(f"{base}/labels")
        async def create_label(request: Request) -> JSONResponse:
            if (denied := authorized(request, GMAIL_MODIFY)) is not None:
                return denied
            name = (await request.json())["name"]
            if name in fake.labels.values():
                return JSONResponse(
                    {"error": {"code": 409, "message": "Label name exists or conflicts"}}, 409
                )
            label_id = fake._next_id("Label_")
            fake.labels[label_id] = name
            return JSONResponse({"id": label_id, "name": name})

        @app.post(f"{base}/messages/{{message_id}}/modify")
        async def modify(request: Request, message_id: str) -> JSONResponse:
            if (denied := authorized(request, GMAIL_MODIFY)) is not None:
                return denied
            body = await request.json()
            if message_id not in fake.messages:
                return JSONResponse({"error": {"code": 404, "message": "Not Found"}}, 404)
            for label_id in [*(body.get("addLabelIds") or []), *(body.get("removeLabelIds") or [])]:
                if label_id not in fake.labels:
                    message = f"Invalid label: {label_id}"
                    return JSONResponse({"error": {"code": 400, "message": message}}, 400)
            fake.relabel(
                message_id,
                add=body.get("addLabelIds") or [],
                remove=body.get("removeLabelIds") or [],
            )
            return JSONResponse(fake.resource(fake.messages[message_id], "minimal"))

        def new_draft(message: dict[str, Any], draft_id: str | None = None) -> dict[str, Any]:
            raw = _unb64(message["raw"])
            stored = fake._store(
                raw, thread_id=message.get("threadId"), labels=["DRAFT"], when=fake.now()
            )
            draft_id = draft_id or fake._next_id("r")
            fake.drafts[draft_id] = {"id": draft_id, "message_id": stored["id"]}
            return {"id": draft_id, "message": {"id": stored["id"], "threadId": stored["threadId"]}}

        @app.post(f"{base}/drafts")
        async def create_draft(request: Request) -> JSONResponse:
            if (denied := authorized(request, GMAIL_MODIFY)) is not None:
                return denied
            return JSONResponse(new_draft((await request.json())["message"]))

        @app.put(f"{base}/drafts/{{draft_id}}")
        async def update_draft(request: Request, draft_id: str) -> JSONResponse:
            if (denied := authorized(request, GMAIL_MODIFY)) is not None:
                return denied
            old = fake.drafts.pop(draft_id, None)
            if old is None:
                return JSONResponse({"error": {"code": 404, "message": "Not Found"}}, 404)
            fake.messages.pop(old["message_id"], None)
            return JSONResponse(new_draft((await request.json())["message"], draft_id))

        @app.get(f"{base}/drafts/{{draft_id}}")
        async def get_draft(request: Request, draft_id: str) -> JSONResponse:
            if (denied := authorized(request)) is not None:
                return denied
            draft = fake.drafts.get(draft_id)
            if draft is None:
                return JSONResponse({"error": {"code": 404, "message": "Not Found"}}, 404)
            return JSONResponse(
                {"id": draft_id, "message": fake.resource(fake.messages[draft["message_id"]])}
            )

        @app.delete(f"{base}/drafts/{{draft_id}}")
        async def delete_draft(request: Request, draft_id: str) -> Response:
            if (denied := authorized(request, GMAIL_MODIFY)) is not None:
                return denied
            draft = fake.drafts.pop(draft_id, None)
            if draft is None:
                return JSONResponse({"error": {"code": 404, "message": "Not Found"}}, 404)
            fake.messages.pop(draft["message_id"], None)
            return Response(status_code=204)

        @app.post(f"{base}/messages/send")
        async def send(request: Request) -> JSONResponse:
            if (denied := authorized(request, GMAIL_MODIFY)) is not None:
                return denied
            if "send_down" in fake.faults:  # Gmail unreachable: nothing was sent
                fake.faults.discard("send_down")
                raise httpx.ConnectError("connection refused")
            if "send_timeout" in fake.faults:  # no answer, and in fact nothing was sent
                fake.faults.discard("send_timeout")
                raise httpx.ReadTimeout("no answer")
            body = await request.json()
            parsed = message_from_bytes(_unb64(body["raw"]), policy=policy.SMTP)
            del parsed["Message-ID"]
            parsed["Message-ID"] = f"<{fake._next_id('g')}@mail.gmail.com>"  # Gmail sets its own
            stored = fake._store(
                parsed.as_bytes(), thread_id=body.get("threadId"), labels=["SENT"], when=fake.now()
            )
            stored["via_api"] = True
            if "send_lost" in fake.faults:  # sent, but the answer never arrives
                fake.faults.discard("send_lost")
                raise httpx.ReadTimeout("the connection dropped")
            if "send_503" in fake.faults:
                fake.faults.discard("send_503")
                return JSONResponse({"error": {"code": 503, "message": "Backend Error"}}, 503)
            return JSONResponse(
                {"id": stored["id"], "threadId": stored["threadId"], "labelIds": ["SENT"]}
            )

        # For browser tests: what actually went out (never part of Google's API)
        @app.get("/__test/sent")
        async def sent_mail() -> JSONResponse:
            items = [m for m in fake.messages.values() if m.get("via_api")]
            return JSONResponse(
                {
                    "count": len(items),
                    "messages": [
                        {
                            "id": m["id"],
                            "threadId": m["threadId"],
                            "to": str(message_from_bytes(m["raw"])["To"]),
                        }
                        for m in items
                    ],
                }
            )

        # Calendar
        @app.post("/calendar/v3/calendars/primary/events")
        async def insert_event(request: Request) -> JSONResponse:
            if (denied := authorized(request, CALENDAR_EVENTS)) is not None:
                return denied
            event = await request.json() | {"id": fake._next_id("e"), "status": "confirmed"}
            fake.events.append(event)
            return JSONResponse(event)

        return app
