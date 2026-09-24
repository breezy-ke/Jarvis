"""The voice brain: one Jarvis for voice and text, with deterministic voice approvals."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test
from sqlalchemy import select

from jarvis.chat.service import ChatEvent, ChatService
from jarvis.clock import FrozenClock
from jarvis.config import REPO_ROOT
from jarvis.db.models import ActionProposal, ChatMessage, Conversation, LLMCall
from jarvis.policy.state import get_kill_switch
from jarvis.services import Services
from jarvis.voice.brain import STOOD_DOWN, JarvisVoiceBrain, VoiceSessionState
from jarvis.voice.config import VoiceConfig, load_voice_config
from tests.integration.fakes import register_test_actions

pytestmark = pytest.mark.db


def voice_config(**conversation: Any) -> VoiceConfig:
    base = load_voice_config(REPO_ROOT / "config" / "voice.yaml")
    return base.model_copy(
        update={"conversation": base.conversation.model_copy(update=conversation)}
    )


def turn(text: str) -> LLMContextFrame:
    return LLMContextFrame(LLMContext(messages=[{"role": "user", "content": text}]))


def propose(kind: str, payload: dict[str, Any]) -> str:
    args = {"kind": kind, "payload": payload, "rationale": "testing voice approvals"}
    return f"/tool propose_action {json.dumps(args)}"


async def run_brain(
    services: Services, frames: list[Frame], state: VoiceSessionState, **conversation: Any
) -> list[Frame]:
    brain = JarvisVoiceBrain(
        services=services,
        chat=ChatService(services),
        config=voice_config(**conversation),
        channel="voice",
        state=state,
    )
    down, _ = await run_test(brain, frames_to_send=frames)
    return list(down)


def events(frames: list[Frame]) -> list[dict[str, Any]]:
    return [f.message for f in frames if isinstance(f, OutputTransportMessageUrgentFrame)]


def spoken(frames: list[Frame]) -> str:
    return "".join(f.text for f in frames if isinstance(f, LLMTextFrame))


async def test_a_voice_turn_is_answered_by_the_same_jarvis(services: Services) -> None:
    state = VoiceSessionState()
    frames = await run_brain(services, [turn("What is on my calendar today?")], state)

    assert isinstance(frames[1], LLMFullResponseStartFrame)
    assert isinstance(frames[-1], LLMFullResponseEndFrame)
    assert spoken(frames).strip() == "Understood. You said: What is on my calendar today?"
    assert {"type": "user", "text": "What is on my calendar today?", "final": True} in events(
        frames
    )
    assert {"type": "state", "state": "thinking"} in events(frames)
    async with services.session_factory() as session:
        conversation = await session.get(Conversation, state.conversation_id)
        assert conversation is not None
        assert conversation.channel == "voice"
        calls = list(await session.scalars(select(LLMCall.task)))
    assert calls == ["voice"]  # the fast voice route, not the chat one


async def test_an_action_is_read_back_and_approved_by_voice(services: Services) -> None:
    register_test_actions(services)
    state = VoiceSessionState()
    request = propose("calendar.invite", {"title": "Kickoff", "attendees": ["a@acme.co.ke"]})
    frames = await run_brain(services, [turn(request), turn("Jarvis, confirm please.")], state)

    said = spoken(frames)
    assert "To confirm: Invite to Kickoff. Say “confirm” to go ahead, or “cancel”." in said
    assert "Approved." in said
    async with services.session_factory() as session:
        proposal = (await session.scalars(select(ActionProposal))).one()
    assert proposal.status == "approved"
    assert proposal.decided_via == "voice"
    assert proposal.approved_hash == proposal.payload_hash
    confirmations = [e for e in events(frames) if e["type"] == "confirmation"]
    assert confirmations[0]["proposal_id"] == str(proposal.id)
    assert confirmations[-1]["proposal_id"] is None
    assert state.pending is None


async def test_cancel_rejects_the_action(services: Services) -> None:
    register_test_actions(services)
    state = VoiceSessionState()
    request = propose("calendar.invite", {"title": "Kickoff", "attendees": ["a@acme.co.ke"]})
    frames = await run_brain(services, [turn(request), turn("Cancel.")], state)
    assert spoken(frames).endswith("Cancelled.")
    async with services.session_factory() as session:
        proposal = (await session.scalars(select(ActionProposal))).one()
    assert proposal.status == "rejected"
    assert proposal.decided_via == "voice"


async def test_anything_else_is_a_new_request_and_the_action_keeps_waiting(
    services: Services,
) -> None:
    register_test_actions(services)
    state = VoiceSessionState()
    request = propose("calendar.invite", {"title": "Kickoff", "attendees": ["a@acme.co.ke"]})
    frames = await run_brain(services, [turn(request), turn("Yes, what time is it?")], state)
    assert "Understood. You said: Yes, what time is it?" in spoken(frames)
    async with services.session_factory() as session:
        proposal = (await session.scalars(select(ActionProposal))).one()
    assert proposal.status == "pending"
    assert state.pending is None


async def test_a_late_confirm_does_nothing(services: Services, clock: FrozenClock) -> None:
    register_test_actions(services)
    state = VoiceSessionState()
    request = propose("calendar.invite", {"title": "Kickoff", "attendees": ["a@acme.co.ke"]})
    await run_brain(services, [turn(request)], state, confirmation_seconds=30)
    assert state.pending is not None
    clock.advance(seconds=31)
    frames = await run_brain(services, [turn("Confirm.")], state)
    assert "Approved" not in spoken(frames)
    async with services.session_factory() as session:
        proposal = (await session.scalars(select(ActionProposal))).one()
    assert proposal.status == "pending"


async def test_high_risk_actions_are_never_approved_by_voice(services: Services) -> None:
    register_test_actions(services)
    state = VoiceSessionState()
    frames = await run_brain(
        services,
        [turn(propose("deploy.production", {"project": "clinic-site"})), turn("Confirm.")],
        state,
    )
    assert "That needs your approval in the app, with your passkey." in spoken(frames)
    async with services.session_factory() as session:
        proposal = (await session.scalars(select(ActionProposal))).one()
    assert proposal.status == "pending"  # "confirm" was just a new request


class SlowChat:
    """A ChatService stand-in whose answer takes a while to start."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def stream_reply(self, text: str, **_: Any) -> AsyncIterator[ChatEvent]:
        yield ChatEvent("start", {"conversation_id": "00000000-0000-0000-0000-000000000001"})
        await asyncio.sleep(self.delay)
        yield ChatEvent("delta", {"text": "Here it is."})


async def test_a_slow_answer_gets_a_filler(services: Services) -> None:
    brain = JarvisVoiceBrain(
        services=services,
        chat=SlowChat(delay=0.6),  # type: ignore[arg-type]
        config=voice_config(filler_after_seconds=0.3, fillers=("One moment.",)),
        channel="voice",
    )
    down, _ = await run_test(brain, frames_to_send=[turn("Plan my week")])
    fillers = [f for f in down if isinstance(f, TTSSpeakFrame)]
    assert [f.text for f in fillers] == ["One moment."]
    assert spoken(list(down)) == "Here it is."


async def test_a_fast_answer_gets_no_filler(services: Services) -> None:
    brain = JarvisVoiceBrain(
        services=services,
        chat=SlowChat(delay=0.0),  # type: ignore[arg-type]
        config=voice_config(filler_after_seconds=0.3),
        channel="voice",
    )
    down, _ = await run_test(brain, frames_to_send=[turn("Hi")])
    assert not [f for f in down if isinstance(f, TTSSpeakFrame)]


class TalkativeChat:
    """Streams a long answer slowly, and records what gets saved as interrupted."""

    def __init__(self) -> None:
        self.saved: list[tuple[uuid.UUID, str]] = []

    async def stream_reply(self, text: str, **_: Any) -> AsyncIterator[ChatEvent]:
        yield ChatEvent("start", {"conversation_id": str(CONVERSATION)})
        for word in ["Your", "week", "starts", "with", "a", "client", "call", "and", "then"]:
            yield ChatEvent("delta", {"text": word + " "})
            await asyncio.sleep(0.05)

    async def save_interrupted(self, conversation_id: uuid.UUID, text: str) -> None:
        self.saved.append((conversation_id, text))


CONVERSATION = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


async def test_barge_in_stops_the_answer_and_keeps_what_was_heard(services: Services) -> None:
    chat = TalkativeChat()
    state = VoiceSessionState()
    brain = JarvisVoiceBrain(
        services=services,
        chat=chat,  # type: ignore[arg-type]
        config=voice_config(filler_after_seconds=5),
        channel="voice",
        state=state,
    )
    down, _ = await run_test(
        brain,
        frames_to_send=[turn("Tell me about my week"), SleepFrame(sleep=0.18), InterruptionFrame()],
    )
    heard = spoken(list(down))
    assert heard.startswith("Your week")
    assert not heard.strip().endswith("and then")  # cut off before the end
    assert chat.saved == [(CONVERSATION, heard)]
    assert state.interrupted_after == heard


async def test_save_interrupted_keeps_the_partial_reply(services: Services) -> None:
    chat = ChatService(services)
    conversation = await chat.create_conversation(channel="voice", title="Week")
    await chat.save_interrupted(conversation.id, "Your week starts with")
    await chat.save_interrupted(conversation.id, "   ")  # nothing heard: nothing saved
    async with services.session_factory() as session:
        messages = list(await session.scalars(select(ChatMessage)))
    assert len(messages) == 1
    assert messages[0].content == "Your week starts with …"
    assert messages[0].meta == {"interrupted": True, "mode": "voice"}


async def test_stand_down_engages_the_kill_switch(services: Services) -> None:
    register_test_actions(services)
    state = VoiceSessionState()
    request = propose("calendar.invite", {"title": "Kickoff", "attendees": ["a@acme.co.ke"]})
    frames = await run_brain(services, [turn(request), turn("Jarvis, stand down!")], state)
    assert spoken(frames).endswith(STOOD_DOWN)
    assert state.pending is None  # the waiting confirmation is dropped too
    async with services.session_factory() as session:
        assert (await get_kill_switch(session)).engaged
        proposal = (await session.scalars(select(ActionProposal))).one()
    assert proposal.status == "pending"  # standing down never approves anything
