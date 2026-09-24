"""Server-sent events for streamed replies."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from sse_starlette.sse import EventSourceResponse

from jarvis.chat.service import ChatEvent


def sse(events: AsyncIterator[ChatEvent]) -> EventSourceResponse:
    async def gen() -> AsyncIterator[dict[str, str]]:
        async for event in events:
            yield {"event": event.type, "data": json.dumps(event.data, ensure_ascii=False)}

    return EventSourceResponse(gen(), ping=15, headers={"Cache-Control": "no-store"})
