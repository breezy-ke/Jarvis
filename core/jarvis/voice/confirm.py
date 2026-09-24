"""Approving actions by voice, without trusting the model.

When a voice turn leaves an action waiting for approval, Jarvis reads back the
action's own summary (built from its payload, not written by the model) and
asks for an exact phrase from `config/voice.yaml`. Only the owner's transcribed
words are checked, as a whole utterance: "say confirm to go ahead" (Jarvis's
own voice picked up by a microphone) never matches "confirm". The approval is
bound to the payload hash that was read back, over the VOICE channel, so the
policy engine still refuses anything high-risk.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import ActionProposal
from jarvis.policy.config import PoliciesConfig
from jarvis.policy.types import Channel, Risk, Status
from jarvis.voice.config import VoiceConfig, normalize_phrase

# Words people wrap around a command: "Jarvis, confirm please" means "confirm".
_WRAPPERS = ("jarvis", "please", "thanks", "thank you", "ok", "okay", "now")


def strip_wrappers(text: str) -> str:
    words = normalize_phrase(text)
    changed = True
    while changed and words:
        changed = False
        for wrapper in _WRAPPERS:
            if words == wrapper:
                return ""
            if words.startswith(wrapper + " "):
                words = words[len(wrapper) + 1 :]
                changed = True
            if words.endswith(" " + wrapper):
                words = words[: -len(wrapper) - 1]
                changed = True
    return words


def classify(text: str, config: VoiceConfig) -> Literal["confirm", "cancel"] | None:
    """What an utterance means for a waiting confirmation, if anything."""
    words = strip_wrappers(text)
    if words in config.confirm_phrases:
        return "confirm"
    if words in config.cancel_phrases:
        return "cancel"
    return None


@dataclass(frozen=True)
class PendingConfirmation:
    proposal_id: uuid.UUID
    payload_hash: str
    summary: str
    expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


def voice_can_approve(policies: PoliciesConfig, risk: str) -> bool:
    try:
        return Channel.VOICE in policies.approval_channels[Risk(risk)]
    except (KeyError, ValueError):
        return False


async def new_pending_proposals(
    session: AsyncSession, conversation_id: uuid.UUID, since: datetime
) -> list[ActionProposal]:
    rows = await session.scalars(
        select(ActionProposal)
        .where(ActionProposal.conversation_id == conversation_id)
        .where(ActionProposal.status == Status.PENDING)
        .where(ActionProposal.created_at >= since)
        .order_by(ActionProposal.created_at)
    )
    return list(rows)


def read_back(
    proposals: list[ActionProposal], policies: PoliciesConfig, config: VoiceConfig, now: datetime
) -> tuple[str, PendingConfirmation | None]:
    """What Jarvis says about new pending actions, and the confirmation it waits for."""
    if not proposals:
        return "", None
    first, rest = proposals[0], len(proposals) - 1
    more = f" There {'is' if rest == 1 else 'are'} {rest} more in the app." if rest else ""
    if not voice_can_approve(policies, first.risk):
        return (
            f"{first.summary}. That needs your approval in the app, with your passkey.{more}",
            None,
        )
    confirm, cancel = config.confirm_phrases[0], config.cancel_phrases[0]
    pending = PendingConfirmation(
        proposal_id=first.id,
        payload_hash=first.payload_hash,
        summary=first.summary,
        expires_at=now + timedelta(seconds=config.conversation.confirmation_seconds),
    )
    speech = f"To confirm: {first.summary}. Say “{confirm}” to go ahead, or “{cancel}”.{more}"
    return speech, pending
