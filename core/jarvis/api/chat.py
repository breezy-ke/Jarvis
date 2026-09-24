"""Chat: streamed replies and conversation history."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from jarvis.api.deps import Owner, State
from jarvis.api.sse import sse
from jarvis.db.models import Conversation

router = APIRouter(prefix="/api", tags=["chat"])


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=8_000)
    conversation_id: uuid.UUID | None = None


@router.post("/chat")
async def chat(body: ChatIn, owner: Owner, state: State) -> EventSourceResponse:
    if body.conversation_id is not None:
        async with state.services.session_factory() as session:
            if await session.get(Conversation, body.conversation_id) is None:
                raise HTTPException(status_code=404, detail="Conversation not found.")
    return sse(state.chat.stream_reply(body.text, conversation_id=body.conversation_id))


@router.get("/conversations")
async def conversations(owner: Owner, state: State) -> list[dict[str, Any]]:
    rows = await state.chat.list_conversations()
    return [
        {
            "id": str(c.id),
            "title": c.title,
            "channel": c.channel,
            "updated_at": c.updated_at.isoformat(),
        }
        for c in rows
    ]


@router.get("/conversations/{conversation_id}/messages")
async def messages(conversation_id: uuid.UUID, owner: Owner, state: State) -> list[dict[str, Any]]:
    rows = await state.chat.messages(conversation_id)
    return [
        {
            "id": str(m.id),
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
            "model": m.model_ref,
            "error": bool(m.meta.get("error")),
        }
        for m in rows
    ]
