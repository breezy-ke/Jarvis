"""Helpers shared by agent tools."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.db.session import transaction
from jarvis.services import Services

if TYPE_CHECKING:
    from jarvis.brief.service import BriefService
    from jarvis.mail.service import MailService
    from jarvis.research.researcher import Researcher


@dataclass
class AgentDeps:
    services: Services
    actor: str
    conversation_id: uuid.UUID | None = None
    redact_sensitive: bool = False  # on channels that aren't end-to-end encrypted
    mail: MailService | None = None  # None when email isn't set up
    brief: BriefService | None = None  # None when the tech brief is off
    research: Researcher | None = None  # None when web research isn't set up
    # Someone else's words (an email, a web page, a forwarded message) are in the context.
    # Then memory stays as it is and every proposal waits for the owner.
    read_untrusted: bool = False


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
