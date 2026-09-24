"""The voice pipeline's "LLM" stage: Jarvis itself.

Pipecat's user aggregator decides when the owner has finished a turn (Silero
VAD + Smart Turn) and hands over the conversation context. This stage takes
the owner's last utterance and answers through the same ChatService as the
app: same memory, same tools, same policy engine, same audit log. It adds only
what voice needs: short spoken answers, a filler if the answer is slow to
start, and deterministic voice confirmation of actions (see confirm.py).
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from jarvis.chat.service import ChatEvent, ChatService
from jarvis.db.session import transaction
from jarvis.policy.engine import PolicyError
from jarvis.policy.state import get_kill_switch, is_stand_down, set_kill_switch
from jarvis.policy.types import Channel
from jarvis.services import Services
from jarvis.voice.config import VoiceConfig
from jarvis.voice.confirm import PendingConfirmation, classify, new_pending_proposals, read_back

log = logging.getLogger("jarvis.voice")

SPOKEN_ERROR = "Sorry, I can't answer right now. {reason}"
STOOD_DOWN = (
    "Standing down. Everything I do on my own is paused until you release the kill "
    "switch in the app."
)


@dataclass
class VoiceSessionState:
    """What one voice session remembers between turns."""

    conversation_id: uuid.UUID | None = None
    pending: PendingConfirmation | None = None
    interrupted_after: str | None = None
    announced: set[uuid.UUID] = field(default_factory=set)  # actions already read back


def last_user_text(context: LLMContext) -> str:
    for message in reversed(context.get_messages()):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content: Any = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            return " ".join(p for p in parts if p).strip()
    return ""


def event(kind: str, **data: Any) -> OutputTransportMessageUrgentFrame:
    """A JSON event for the client (transcripts, state, errors)."""
    return OutputTransportMessageUrgentFrame(message={"type": kind, **data})


class JarvisVoiceBrain(FrameProcessor):
    def __init__(
        self,
        *,
        services: Services,
        chat: ChatService,
        config: VoiceConfig,
        channel: str,
        state: VoiceSessionState | None = None,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(name="JarvisVoiceBrain")
        self._s = services
        self._chat = chat
        self._config = config
        self._channel = channel
        self.state = state or VoiceSessionState()
        self._rng = rng or random.Random()  # noqa: S311 (choosing a filler phrase)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            text = last_user_text(frame.context)
            if text:
                await self._respond(text)
        else:
            await self.push_frame(frame, direction)

    # --- One turn ----------------------------------------------------------------

    async def _respond(self, text: str) -> None:
        await self.push_frame(event("user", text=text, final=True))
        await self.push_frame(LLMFullResponseStartFrame())
        try:
            if is_stand_down(text):
                await self._stand_down()
                return
            if await self._handle_confirmation(text):
                return
            await self._answer(text)
        finally:
            await self.push_frame(LLMFullResponseEndFrame())

    async def _stand_down(self) -> None:
        """ "Jarvis, stand down": the spoken kill switch. It never needs confirming."""
        if self.state.pending is not None:
            self.state.pending = None
            await self.push_frame(event("confirmation", proposal_id=None, summary=None))
        async with transaction(self._s.session_factory) as session:
            await set_kill_switch(
                session,
                engaged=True,
                reason="Stood down by voice",
                actor="owner:voice",
                audit=self._s.audit,
                clock=self._s.clock,
            )
        await self._say(STOOD_DOWN)

    async def _say(self, text: str) -> None:
        await self.push_frame(event("assistant", text=text, final=True))
        await self.push_frame(LLMTextFrame(text))

    async def _answer(self, text: str) -> None:
        state = self.state
        note = None
        if state.interrupted_after:
            note = (
                "The owner interrupted your previous spoken answer after: "
                f"“{state.interrupted_after}”. Don't repeat it unless asked."
            )
            state.interrupted_after = None
        await self.push_frame(event("state", state="thinking"))
        turn_started = self._s.clock.now()
        spoken: list[str] = []
        filler = self.create_task(self._filler_after_delay(spoken), name="voice-filler")
        reply = self._chat.stream_reply(
            text,
            conversation_id=state.conversation_id,
            channel=self._channel,
            mode="voice",
            extra_instructions=note,
        )
        try:
            async with aclosing(reply) as stream:
                async for chat_event in stream:
                    await self._on_chat_event(chat_event, spoken, filler)
        except asyncio.CancelledError:
            # The owner talked over the answer (barge-in): keep what they heard.
            if spoken and state.conversation_id is not None:
                heard = "".join(spoken)
                state.interrupted_after = heard[-300:]
                await asyncio.shield(self._chat.save_interrupted(state.conversation_id, heard))
            raise
        finally:
            filler.cancel()
        await self.push_frame(event("assistant", text="", final=True))
        if state.conversation_id is not None:
            await self._read_back_new_actions(state.conversation_id, turn_started)

    async def _on_chat_event(
        self, chat_event: ChatEvent, spoken: list[str], filler: asyncio.Task[None]
    ) -> None:
        if chat_event.type == "start":
            self.state.conversation_id = uuid.UUID(chat_event.data["conversation_id"])
            await self.push_frame(event("conversation", id=chat_event.data["conversation_id"]))
        elif chat_event.type == "delta":
            filler.cancel()
            spoken.append(chat_event.data["text"])
            await self.push_frame(event("assistant", text=chat_event.data["text"]))
            await self.push_frame(LLMTextFrame(chat_event.data["text"]))
        elif chat_event.type == "error":
            filler.cancel()
            reason = _first_sentence(chat_event.data["message"])
            await self._say(SPOKEN_ERROR.format(reason=reason))

    async def _filler_after_delay(self, spoken: list[str]) -> None:
        conversation = self._config.conversation
        await asyncio.sleep(conversation.filler_after_seconds)
        if not spoken and conversation.fillers:
            phrase = self._rng.choice(conversation.fillers)
            await self.push_frame(event("filler", text=phrase))
            await self.push_frame(TTSSpeakFrame(phrase))

    # --- Voice confirmation of actions -------------------------------------------------

    async def _read_back_new_actions(self, conversation_id: uuid.UUID, since: Any) -> None:
        async with self._s.session_factory() as session:
            proposals = await new_pending_proposals(session, conversation_id, since)
        proposals = [p for p in proposals if p.id not in self.state.announced]
        self.state.announced.update(p.id for p in proposals)
        speech, pending = read_back(
            proposals, self._s.policies_config, self._config, self._s.clock.now()
        )
        if speech:
            self.state.pending = pending
            await self._say(speech)
            if pending is not None:
                await self.push_frame(
                    event(
                        "confirmation",
                        proposal_id=str(pending.proposal_id),
                        summary=pending.summary,
                    )
                )

    async def _handle_confirmation(self, text: str) -> bool:
        """Settle a waiting confirmation. True if this utterance was the answer."""
        pending = self.state.pending
        if pending is None:
            return False
        self.state.pending = None
        if pending.expired(self._s.clock.now()):
            return False
        decision = classify(text, self._config)
        if decision is None:
            # Not an answer: treat it as a new request. The action stays in the app.
            await self.push_frame(event("confirmation", proposal_id=None, summary=None))
            return False
        try:
            if decision == "cancel":
                reply = await self._cancel(pending)
            else:
                reply = await self._approve(pending)
        except PolicyError as exc:
            reply = f"I couldn't do that: {_first_sentence(str(exc))}"
        await self.push_frame(event("confirmation", proposal_id=None, summary=None))
        await self._say(reply)
        return True

    async def _approve(self, pending: PendingConfirmation) -> str:
        async with transaction(self._s.session_factory) as session:
            proposal = await self._s.policy.approve(
                session,
                pending.proposal_id,
                approved_hash=pending.payload_hash,
                channel=Channel.VOICE,
                actor="owner:voice",
            )
            killed = (await get_kill_switch(session)).engaged
        if killed:
            return "Approved. The kill switch is on, so it waits until you release it."
        delay = 0
        if proposal.execute_after is not None:
            delay = max(0, round((proposal.execute_after - self._s.clock.now()).total_seconds()))
        if delay >= 5:
            return f"Approved. It goes in {delay} seconds; you can undo it in the app."
        return "Approved."

    async def _cancel(self, pending: PendingConfirmation) -> str:
        async with transaction(self._s.session_factory) as session:
            await self._s.policy.reject(
                session,
                pending.proposal_id,
                channel=Channel.VOICE,
                reason="Cancelled by voice",
                actor="owner:voice",
            )
        return "Cancelled."


def _first_sentence(text: str) -> str:
    sentence = text.strip().split("\n", 1)[0]
    for stop in (". ", "? ", "! "):
        if stop in sentence:
            sentence = sentence.split(stop, 1)[0] + stop.strip()
            break
    return sentence[:200]
