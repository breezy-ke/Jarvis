"""After a conversation goes quiet, extract durable facts into memory.

Everything extracted here is stored as `inferred`. The owner confirms or
rejects it on the memory page, so a model's misreading can never become a
"known fact" without the owner seeing it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from sqlalchemy import func, or_, select

from jarvis.db.models import BENCHMARK_CHANNEL, ChatMessage, Conversation
from jarvis.db.session import transaction
from jarvis.memory.store import FactInput, FactStatus, MemoryStoreError
from jarvis.services import Services

FactCategory = Literal[
    "identity",
    "business",
    "client",
    "project",
    "preference",
    "contact",
    "schedule",
    "goal",
    "skill",
    "note",
]


class MemoryOp(BaseModel):
    op: Literal["add", "invalidate"] = Field(
        description=(
            "'add' a new or changed fact; 'invalidate' one the owner said is no longer true."
        )
    )
    category: FactCategory
    subject: str = Field(description="'owner' for the owner; otherwise the person/company/thing.")
    predicate: str = Field(description="Short relationship, e.g. 'prefers', 'budget', 'works at'.")
    value: str = Field(default="", description="The fact's value (empty for 'invalidate').")
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(description="A short quote from the OWNER's messages supporting this.")
    sensitive: bool = Field(default=False, description="Health, finances, family, credentials...")


class ExtractionResult(BaseModel):
    operations: list[MemoryOp] = Field(default_factory=list)
    episode_summary: str = Field(
        default="",
        description="One or two sentences on what this conversation was about ('' if trivial).",
    )


EXTRACTION_INSTRUCTIONS = """\
You maintain the long-term memory of a personal assistant.
From the conversation below, extract DURABLE facts about the owner and their world:
clients, projects, preferences, contacts, schedule, goals, skills, business details.

Rules:
- Only use what the OWNER said. Assistant messages are context, never evidence.
- Skip one-off requests, pleasantries and anything already obvious from the question itself.
- Use subject "owner" for the owner.
- If the owner says something is no longer true, emit an 'invalidate' operation.
- Every operation needs a short evidence quote from the owner.
- Mark health, money, family and credential details as sensitive.
- Never extract secrets such as passwords or API keys.
- If nothing durable was said, return no operations.
"""


def build_extraction_agent() -> Agent[None, ExtractionResult]:
    return Agent(
        output_type=ExtractionResult, instructions=EXTRACTION_INSTRUCTIONS, name="extractor"
    )


async def extract_conversation(
    services: Services, agent: Agent[None, ExtractionResult], conversation_id: uuid.UUID
) -> int:
    """Extract memories from one conversation's new messages. Returns facts written."""
    async with services.session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return 0
        since = conversation.memory_extracted_at or datetime.min.replace(
            tzinfo=services.clock.now().tzinfo
        )
        messages = list(
            await session.scalars(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .where(ChatMessage.created_at > since)
                .order_by(ChatMessage.created_at)
            )
        )
    owner_turns = [m for m in messages if m.role == "user"]
    written = 0
    if owner_turns:
        transcript = "\n".join(
            f"{'OWNER' if m.role == 'user' else 'ASSISTANT'}: {m.content[:2000]}"
            for m in messages[-40:]
        )
        outcome = await services.router.run(agent, transcript, task="extraction")
        result: ExtractionResult = outcome.output
        async with transaction(services.session_factory) as session:
            for op in result.operations[:25]:
                try:
                    if op.op == "invalidate":
                        written += await services.memory.retire(
                            session,
                            subject=op.subject,
                            predicate=op.predicate,
                            actor="agent:extractor",
                        )
                        continue
                    if not op.value.strip():
                        continue
                    await services.memory.add(
                        session,
                        FactInput(
                            category=op.category,
                            subject=op.subject,
                            predicate=op.predicate,
                            value=op.value,
                            source="conversation",
                            source_ref=str(conversation_id),
                            status=FactStatus.INFERRED,
                            confidence=op.confidence,
                            sensitivity="sensitive" if op.sensitive else "normal",
                        ),
                        actor="agent:extractor",
                    )
                    written += 1
                except MemoryStoreError:
                    continue
            if result.episode_summary.strip():
                await services.memory.add_episode(
                    session, summary=result.episode_summary, conversation_id=conversation_id
                )
    async with transaction(services.session_factory) as session:
        conversation = await session.get(Conversation, conversation_id, with_for_update=True)
        if conversation is not None:
            conversation.memory_extracted_at = services.clock.now()
    return written


async def extract_idle_conversations(
    services: Services,
    agent: Agent[None, ExtractionResult],
    *,
    idle_for: timedelta = timedelta(minutes=10),
    limit: int = 10,
) -> int:
    """Run extraction for conversations that changed and have been quiet a while."""
    now = services.clock.now()
    async with services.session_factory() as session:
        ids = list(
            await session.scalars(
                select(Conversation.id)
                .where(
                    or_(
                        Conversation.memory_extracted_at.is_(None),
                        Conversation.updated_at > Conversation.memory_extracted_at,
                    )
                )
                .where(Conversation.updated_at < now - idle_for)
                .where(Conversation.channel != BENCHMARK_CHANNEL)
                .order_by(func.coalesce(Conversation.memory_extracted_at, Conversation.created_at))
                .limit(limit)
            )
        )
    total = 0
    for conversation_id in ids:
        total += await extract_conversation(services, agent, conversation_id)
    return total
