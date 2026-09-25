"""Helpers shared by agent tools."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.db.session import transaction
from jarvis.services import Services

if TYPE_CHECKING:
    from jarvis.mail.service import MailService


@dataclass
class AgentDeps:
    services: Services
    actor: str
    conversation_id: uuid.UUID | None = None
    redact_sensitive: bool = False  # on channels that aren't end-to-end encrypted
    mail: MailService | None = None  # None when email isn't set up


async def audit_tool(
    deps: AgentDeps, tool: str, summary: str, data: dict[str, Any] | None = None
) -> None:
    """Record a tool call. Metadata only, never the content."""
    async with transaction(deps.services.session_factory) as session:
        await deps.services.audit.append(
            session,
            actor=deps.actor,
            event_type="tool.call",
            subject_type="conversation" if deps.conversation_id else None,
            subject_id=str(deps.conversation_id) if deps.conversation_id else None,
            summary=summary,
            data={"tool": tool, **(data or {})},
        )
