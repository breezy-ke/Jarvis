"""Conversations: persistence, context building, and streamed replies."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from sqlalchemy import func, select

from jarvis.agents.orchestrator import build_orchestrator
from jarvis.agents.tools import AgentDeps
from jarvis.db.models import ActionProposal, ChatMessage, Conversation, ConversationTurn
from jarvis.db.session import transaction
from jarvis.llm.router import RouterError, StreamDone, TextDelta
from jarvis.memory.retrieval import render_facts, search_facts
from jarvis.policy.state import get_kill_switch
from jarvis.policy.types import Status
from jarvis.profile.service import core_summary
from jarvis.services import Services

HISTORY_TURNS = 12

UNENCRYPTED_CHANNELS = frozenset({"telegram"})
"""Channels whose messages pass through someone else's servers in readable form.

On these, Jarvis leaves sensitive memories and the personal profile section out
of the model's context, and the channel masks anything secret-looking on the way
out (see jarvis/telegram)."""

PRIVACY_RULES = """
## This chat isn't private
The owner is writing from a chat app that isn't end-to-end encrypted. So:
- Never write passwords, keys, tokens, full card, bank or ID numbers, or health,
  money or family details. Say those are in the Jarvis app instead.
- Sensitive memories are hidden from you here. If you can't find something,
  suggest the app rather than guessing.
"""

TELEGRAM_STYLE = """
## Telegram
- Keep replies skimmable: short paragraphs and simple lists. No tables.
- You can't approve actions yourself. When you propose one, Jarvis sends the
  owner Approve and Reject buttons (high-risk actions need the app and a passkey).
"""

VOICE_STYLE = """
## Voice mode
The owner is talking to you and hears your reply spoken aloud. So:
- Answer in one to three short sentences. If more is needed, give the gist and
  offer to put the details in the app.
- Plain speech only: no markdown, lists, tables, code, emoji or URLs.
- Say numbers, dates and times the way a person would say them.
- If you proposed an action, say what it is in one sentence; Jarvis will ask
  the owner to confirm it.
"""


@dataclass(frozen=True)
class ChatEvent:
    type: str  # "start" | "delta" | "done" | "error"
    data: dict[str, Any]


class ConversationNotFound(LookupError):
    pass


class ChatService:
    def __init__(self, services: Services, *, agent: Agent[AgentDeps, str] | None = None) -> None:
        self._s = services
        self.agent = agent or build_orchestrator(services.persona)

    async def create_conversation(
        self, *, channel: str = "pwa", title: str | None = None
    ) -> Conversation:
        now = self._s.clock.now()
        async with transaction(self._s.session_factory) as session:
            conversation = Conversation(
                id=uuid.uuid4(),
                title=title or "New conversation",
                channel=channel,
                created_at=now,
                updated_at=now,
            )
            session.add(conversation)
        return conversation

    async def _history(self, conversation_id: uuid.UUID) -> list[ModelMessage]:
        async with self._s.session_factory() as session:
            turns = list(
                await session.scalars(
                    select(ConversationTurn)
                    .where(ConversationTurn.conversation_id == conversation_id)
                    .order_by(ConversationTurn.id.desc())
                    .limit(HISTORY_TURNS)
                )
            )
        messages: list[ModelMessage] = []
        for turn in reversed(turns):
            messages.extend(ModelMessagesTypeAdapter.validate_python(turn.model_messages))
        return messages

    async def build_context(self, user_text: str, *, redact: bool = False) -> str:
        """Per-turn context: time, profile summary, relevant memories, system state.

        With `redact`, sensitive memories and the personal profile section are left
        out, so the model can't repeat them on a channel that isn't private.
        """
        s = self._s
        now_local = s.clock.now().astimezone(s.policies_config.tz)
        async with s.session_factory() as session:
            snapshot = await s.profiles.current(session)
            gate = await s.profiles.status(session)
            kill = await get_kill_switch(session)
            pending = await session.scalar(
                select(func.count())
                .select_from(ActionProposal)
                .where(ActionProposal.status == Status.PENDING)
            )
            facts = await search_facts(
                session,
                s.embedder,
                user_text,
                now=s.clock.now(),
                limit=6,
                exclude_sensitive=redact,
            )
        tz_name = s.policies_config.defaults.timezone
        profile = core_summary(snapshot.profile, exclude=("personal",) if redact else ())
        context = (
            f"## Right now\n{now_local:%A %d %B %Y, %H:%M} ({tz_name}). "
            f"Pending approvals: {pending or 0}. Kill switch: {'ON' if kill.engaged else 'off'}. "
            f"Autonomy: {'on' if gate.open else 'off'} ({gate.reason}).\n\n"
            f"## Owner profile\n{profile}\n\n"
            f"## Possibly relevant memories\n{render_facts(facts)}"
        )
        if redact:
            context += PRIVACY_RULES
            topics = snapshot.profile.boundaries.sensitive_topics
            if topics:
                context += f"- Don't discuss these topics here: {'; '.join(topics)}.\n"
        return context

    async def stream_reply(
        self,
        text: str,
        *,
        conversation_id: uuid.UUID | None = None,
        channel: str = "pwa",
        mode: Literal["text", "voice"] = "text",
        extra_instructions: str | None = None,
    ) -> AsyncGenerator[ChatEvent, None]:
        """Stream Jarvis's reply to `text`, saving both sides of the exchange.

        Voice mode uses the `voice` model task (fast, no reasoning pause) and asks
        for short spoken answers. If the owner talks over a voice reply, the voice
        pipeline keeps what was already said via `save_interrupted`.
        """
        s = self._s
        text = text.strip()
        if conversation_id is None:
            conversation = await self.create_conversation(channel=channel, title=text[:80])
            conversation_id = conversation.id
        else:
            async with s.session_factory() as session:
                if await session.get(Conversation, conversation_id) is None:
                    raise ConversationNotFound(str(conversation_id))

        now = s.clock.now()
        async with transaction(s.session_factory) as session:
            session.add(
                ChatMessage(
                    id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    role="user",
                    content=text,
                    created_at=now,
                )
            )
        yield ChatEvent("start", {"conversation_id": str(conversation_id)})

        history = await self._history(conversation_id)
        redact = channel in UNENCRYPTED_CHANNELS
        context = await self.build_context(text, redact=redact)
        if channel == "telegram":
            context += TELEGRAM_STYLE
        if mode == "voice":
            context += VOICE_STYLE
        if extra_instructions:
            context += "\n" + extra_instructions
        deps = AgentDeps(
            services=s,
            actor="agent:jarvis",
            conversation_id=conversation_id,
            redact_sensitive=redact,
        )
        chunks: list[str] = []
        try:
            async for event in s.router.stream(
                self.agent,
                text,
                task="voice" if mode == "voice" else "chat",
                deps=deps,
                message_history=history,
                instructions=context,
            ):
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                    yield ChatEvent("delta", {"text": event.text})
                elif isinstance(event, StreamDone):
                    await self._save_reply(conversation_id, event, "".join(chunks))
                    yield ChatEvent(
                        "done",
                        {
                            "conversation_id": str(conversation_id),
                            "model": event.model_ref,
                            "text": event.output,
                        },
                    )
        except RouterError as exc:
            message = str(exc)
            async with transaction(s.session_factory) as session:
                session.add(
                    ChatMessage(
                        id=uuid.uuid4(),
                        conversation_id=conversation_id,
                        role="assistant",
                        content=f"⚠️ {message}",
                        created_at=s.clock.now(),
                        meta={"error": True},
                    )
                )
            yield ChatEvent("error", {"message": message})

    async def save_interrupted(
        self, conversation_id: uuid.UUID, text: str, *, mode: str = "voice"
    ) -> None:
        """Keep the part of a reply the owner heard before talking over it."""
        if not text.strip():
            return
        async with transaction(self._s.session_factory) as session:
            session.add(
                ChatMessage(
                    id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    role="assistant",
                    content=text.strip() + " …",
                    created_at=self._s.clock.now(),
                    meta={"interrupted": True, "mode": mode},
                )
            )

    async def _save_reply(
        self, conversation_id: uuid.UUID, done: StreamDone, streamed: str
    ) -> None:
        s = self._s
        now = s.clock.now()
        async with transaction(s.session_factory) as session:
            session.add(
                ChatMessage(
                    id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    role="assistant",
                    content=done.output or streamed,
                    created_at=now,
                    model_ref=done.model_ref,
                    meta={
                        "input_tokens": done.usage.input_tokens,
                        "output_tokens": done.usage.output_tokens,
                    },
                )
            )
            session.add(
                ConversationTurn(
                    conversation_id=conversation_id,
                    created_at=now,
                    model_messages=ModelMessagesTypeAdapter.dump_python(
                        done.new_messages, mode="json"
                    ),
                )
            )
            conversation = await session.get(Conversation, conversation_id, with_for_update=True)
            if conversation is not None:
                conversation.updated_at = now

    async def list_conversations(self, *, limit: int = 50) -> list[Conversation]:
        async with self._s.session_factory() as session:
            rows = await session.scalars(
                select(Conversation)
                .where(Conversation.archived.is_(False))
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
            )
            return list(rows)

    async def messages(self, conversation_id: uuid.UUID) -> list[ChatMessage]:
        async with self._s.session_factory() as session:
            rows = await session.scalars(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.created_at)
            )
            return list(rows)
