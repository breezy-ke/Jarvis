from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from jarvis.agents.extraction import (
    build_extraction_agent,
    extract_conversation,
    extract_idle_conversations,
)
from jarvis.chat.service import ChatEvent, ChatService, ConversationNotFound
from jarvis.clock import FrozenClock
from jarvis.db.models import (
    BENCHMARK_CHANNEL,
    ActionProposal,
    ChatMessage,
    Conversation,
    ConversationTurn,
    Episode,
    Fact,
)
from jarvis.memory.store import FactStatus
from jarvis.onboarding.modules import MODULES
from jarvis.onboarding.service import OnboardingService
from jarvis.policy.types import Status
from jarvis.services import Services

pytestmark = pytest.mark.db


async def collect(stream: Any) -> list[ChatEvent]:
    return [event async for event in stream]


def text_of(events: list[ChatEvent]) -> str:
    return "".join(e.data["text"] for e in events if e.type == "delta")


# --- Chat ----------------------------------------------------------------------------------


async def test_chat_streams_and_persists(services: Services) -> None:
    chat = ChatService(services)
    events = await collect(chat.stream_reply("Hello Jarvis"))
    assert [events[0].type, events[-1].type] == ["start", "done"]
    assert "You said: Hello Jarvis" in text_of(events)
    conversation_id = events[0].data["conversation_id"]

    follow_up = await collect(
        chat.stream_reply("Second message", conversation_id=events[-1].data["conversation_id"])
    )
    assert follow_up[-1].data["conversation_id"] == conversation_id
    async with services.session_factory() as session:
        roles = [
            m.role
            for m in await session.scalars(select(ChatMessage).order_by(ChatMessage.created_at))
        ]
        turns = list(await session.scalars(select(ConversationTurn)))
    assert roles == ["user", "assistant", "user", "assistant"]
    assert len(turns) == 2


async def test_unknown_conversation(services: Services) -> None:
    import uuid

    chat = ChatService(services)
    with pytest.raises(ConversationNotFound):
        await collect(chat.stream_reply("hi", conversation_id=uuid.uuid4()))


async def test_remember_tool_saves_a_confirmed_fact(services: Services) -> None:
    chat = ChatService(services)
    args = {
        "subject": "owner",
        "predicate": "favourite editor",
        "value": "Neovim",
        "category": "preference",
    }
    events = await collect(chat.stream_reply(f"/tool remember {json.dumps(args)}"))
    assert events[-1].type == "done"
    async with services.session_factory() as session:
        facts = list(await session.scalars(select(Fact)))
    assert [(f.value, f.status) for f in facts] == [("Neovim", FactStatus.CONFIRMED)]


async def test_agent_actions_wait_for_approval_before_onboarding(services: Services) -> None:
    chat = ChatService(services)
    args = {
        "kind": "notify.owner",
        "payload": {"title": "Test", "body": "Hello from Jarvis"},
        "rationale": "testing",
    }
    events = await collect(chat.stream_reply(f"/tool propose_action {json.dumps(args)}"))
    assert "Waiting for the owner's approval" in events[-1].data["text"]
    async with services.session_factory() as session:
        proposal = await session.scalar(select(ActionProposal))
    assert proposal is not None
    assert proposal.status == Status.PENDING
    assert "Autonomy paused" in (proposal.status_reason or "")


async def test_unavailable_capabilities_are_reported_honestly(services: Services) -> None:
    chat = ChatService(services)
    args = {"kind": "email.send", "payload": {"to": ["a@b.co"]}, "rationale": "x"}
    events = await collect(chat.stream_reply(f"/tool propose_action {json.dumps(args)}"))
    assert "isn't available yet" in events[-1].data["text"]


async def test_context_includes_profile_and_memories(services: Services) -> None:
    chat = ChatService(services)
    context = await chat.build_context("anything")
    assert "Owner profile" in context
    assert "Autonomy: off" in context


# --- Memory extraction ----------------------------------------------------------------------


def extraction_model(result: dict[str, Any]) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, result)])

    return FunctionModel(fn)


async def test_extraction_stores_inferred_facts_and_an_episode(
    services: Services, clock: FrozenClock
) -> None:
    chat = ChatService(services)
    events = await collect(
        chat.stream_reply("I've switched from Laravel to Next.js for new clients")
    )
    conversation_id = events[0].data["conversation_id"]
    services.router._models["fake-local"] = extraction_model(
        {
            "operations": [
                {
                    "op": "add",
                    "category": "preference",
                    "subject": "owner",
                    "predicate": "stack for new clients",
                    "value": "Next.js",
                    "confidence": 0.9,
                    "evidence": "switched from Laravel to Next.js",
                }
            ],
            "episode_summary": "Owner moved new client work to Next.js.",
        }
    )
    import uuid

    written = await extract_conversation(
        services, build_extraction_agent(), uuid.UUID(conversation_id)
    )
    assert written == 1
    async with services.session_factory() as session:
        fact = await session.scalar(select(Fact))
        episode = await session.scalar(select(Episode))
    assert fact is not None
    assert fact.status == FactStatus.INFERRED  # never auto-confirmed
    assert episode is not None


async def test_idle_extraction_waits_for_quiet(services: Services, clock: FrozenClock) -> None:
    chat = ChatService(services)
    await collect(chat.stream_reply("note this"))
    agent = build_extraction_agent()
    async with services.session_factory() as session:
        conversation = await session.scalar(select(Conversation))
    assert conversation is not None
    assert await extract_idle_conversations(services, agent) == 0  # not idle yet
    async with services.session_factory() as session:
        assert (await session.get(Conversation, conversation.id)).memory_extracted_at is None  # type: ignore[union-attr]
    clock.advance(minutes=11)
    await extract_idle_conversations(services, agent)
    async with services.session_factory() as session:
        extracted = await session.get(Conversation, conversation.id)
    assert extracted is not None
    assert extracted.memory_extracted_at is not None
    # Already extracted: nothing to do until the conversation changes again.
    assert await extract_idle_conversations(services, agent, idle_for=timedelta(0)) == 0


async def test_benchmark_conversations_never_become_memories(
    services: Services, clock: FrozenClock
) -> None:
    chat = ChatService(services)
    await collect(chat.stream_reply("What is on my calendar today?", channel=BENCHMARK_CHANNEL))
    clock.advance(minutes=11)
    await extract_idle_conversations(services, build_extraction_agent())
    async with services.session_factory() as session:
        conversation = await session.scalar(select(Conversation))
    assert conversation is not None
    assert conversation.channel == BENCHMARK_CHANNEL
    assert conversation.memory_extracted_at is None  # never even looked at


# --- Onboarding ----------------------------------------------------------------------------


async def test_onboarding_opens_saves_and_finishes(services: Services) -> None:
    onboarding = OnboardingService(services)
    opening = await collect(onboarding.stream_turn("identity", None))
    assert opening[-1].type == "done"

    value = json.dumps("Brian")
    await collect(
        onboarding.stream_turn(
            "identity",
            f"/tool save_answer {json.dumps({'field': 'identity.preferred_name', 'value_json': value})}",
        )
    )
    async with services.session_factory() as session:
        snapshot = await services.profiles.current(session)
    assert snapshot.profile.identity.preferred_name == "Brian"

    refused = await collect(
        onboarding.stream_turn(
            "identity",
            '/tool save_answer {"field": "business.name", "value_json": "\\"Nope\\""}',
        )
    )
    assert "couldn't do that" in refused[-1].data["text"]

    await collect(
        onboarding.stream_turn("identity", '/tool finish_module {"summary": "Name noted."}')
    )
    statuses = {s.id: s for s in await onboarding.statuses()}
    assert statuses["identity"].status == "completed"
    assert "identity.location" in statuses["identity"].required_missing
    transcript = await onboarding.transcript("identity")
    assert transcript[0]["role"] == "assistant"


async def test_list_values_are_wrapped(services: Services) -> None:
    onboarding = OnboardingService(services)
    args = {"field": "identity.languages", "value_json": json.dumps("English")}
    await collect(onboarding.stream_turn("identity", f"/tool save_answer {json.dumps(args)}"))
    async with services.session_factory() as session:
        snapshot = await services.profiles.current(session)
    assert snapshot.profile.identity.languages == ["English"]


async def test_overview_and_skip(services: Services) -> None:
    onboarding = OnboardingService(services)
    overview = await onboarding.overview()
    assert overview["modules_total"] == len(MODULES) == 12
    assert overview["gate_open"] is False
    await onboarding.skip("personal")
    statuses = {s.id: s.status for s in await onboarding.statuses()}
    assert statuses["personal"] == "skipped"
    with pytest.raises(KeyError):
        await onboarding.skip("nope")
