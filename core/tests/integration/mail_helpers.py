"""Shared pieces for the email tests: a fake Gmail wired to real Jarvis parts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from jarvis.clock import FrozenClock
from jarvis.db.session import transaction
from jarvis.ingestion.google import GoogleAuth, GoogleEndpoints
from jarvis.llm.fake import fake_model
from jarvis.mail.gmail import GmailClient
from jarvis.mail.store import MailStore
from jarvis.mail.sync import MailAccess, MailSync
from jarvis.services import Services
from tests.fake_gmail import FakeGmail

ENDPOINTS = GoogleEndpoints.fake("http://google.test")
OWNER = "owner@example.com"
REDIRECT_URI = "http://127.0.0.1:8080/api/onboarding/google/callback"


@dataclass
class MailRig:
    services: Services
    clock: FrozenClock
    fake: FakeGmail
    http: httpx.AsyncClient
    store: MailStore
    sync: MailSync
    access: MailAccess

    def client(self) -> GmailClient:
        async def token() -> str:
            return "fake-access"

        return GmailClient(self.http, token, endpoints=ENDPOINTS, backoff=0.0)


async def connect_google(rig: MailRig) -> GoogleAuth:
    """Connect Google the way you would: consent screen, then the callback."""
    google = GoogleAuth(
        client_id="fake-client",
        client_secret="fake-secret",
        redirect_uri=REDIRECT_URI,
        vault=rig.services.vault,
        clock=rig.clock,
        http=rig.http,
        endpoints=ENDPOINTS,
    )
    async with transaction(rig.services.session_factory) as session:
        url = await google.start(session)
    consent = await rig.http.get(url)
    callback = httpx.URL(consent.headers["location"])
    async with transaction(rig.services.session_factory) as session:
        await google.finish(session, state=callback.params["state"], code=callback.params["code"])
    return google


@dataclass
class ScriptedMail:
    """Stands in for the triage and drafting models, and remembers what they were asked.

    Both run through the router's "fake-local" model; which one is asking shows
    in the shape of the answer it wants. The chat agent (free text) gets the
    standard fake model, so `/tool name {json}` still calls a tool.
    """

    triage: dict[str, Any] = field(
        default_factory=lambda: {
            "category": "needs_reply",
            "priority": 3,
            "needs_reply": True,
            "summary": "Asks to meet on Tuesday at 10:00.",
        }
    )
    reply: str = "Tuesday at 10:00 works for me. See you then.\n\nBest regards,\nBrian"
    prompts: list[str] = field(default_factory=list)
    drafting_prompts: list[str] = field(default_factory=list)
    tools_offered: list[list[str]] = field(default_factory=list)
    _chat: FunctionModel = field(default_factory=fake_model, init=False, repr=False)

    def install(self, services: Services) -> ScriptedMail:
        services.router._models["fake-local"] = FunctionModel(self, stream_function=self._stream)
        return self

    async def _stream(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        assert self._chat.stream_function is not None
        async for chunk in self._chat.stream_function(messages, info):
            yield chunk

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.allow_text_output:  # the chat agent
            assert self._chat.function is not None
            return self._chat.function(messages, info)  # type: ignore[return-value]
        output = info.output_tools[0]
        prompt = str(messages[-1])
        self.prompts.append(prompt)
        self.tools_offered.append([t.name for t in info.function_tools])
        if "category" in output.parameters_json_schema.get("properties", {}):
            answer: dict[str, Any] = self.triage
        else:
            self.drafting_prompts.append(prompt)
            answer = {"body": self.reply}
        return ModelResponse(parts=[ToolCallPart(output.name, answer)])
