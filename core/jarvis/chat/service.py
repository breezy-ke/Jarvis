"""Conversations: persistence, context building, and streamed replies."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

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

    async def build_context(self, user_text: str) -> str:
        """Per-turn context: time, profile summary, relevant memories, system state."""
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
            facts = await search_facts(session, s.embedder, user_text, now=s.clock.now(), limit=6)
        tz_name = s.policies_config.defaults.timezone
        return (
            f"## Right now\n{now_local:%A %d %B %Y, %H:%M} ({tz_name}). "
            f"Pending approvals: {pending or 0}. Kill switch: {'ON' if kill.engaged else 'off'}. "
            f"Autonomy: {'on' if gate.open else 'off'} ({gate.reason}).\n\n"
            f"## Owner profile\n{core_summary(snapshot.profile)}\n\n"
            f"## Possibly relevant memories\n{render_facts(facts)}"
        )

    async def stream_reply(
        self, text: str, *, conversation_id: uuid.UUID | None = None, channel: str = "pwa"
    ) -> AsyncIterator[ChatEvent]:
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
        context = await self.build_context(text)
        deps = AgentDeps(services=s, actor="agent:jarvis", conversation_id=conversation_id)
        chunks: list[str] = []
        try:
            async for event in s.router.stream(
                self.agent,
                text,
                task="chat",
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
