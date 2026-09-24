"""Built-in action kinds: payload schemas and executors.

Each new capability (email.send in Phase 3, outreach.send in Phase 5, ...)
registers here. Its policy comes from config/policies.yaml.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jarvis.notify.push import PushService
from jarvis.policy.registry import ActionRegistry, ActionSpec, ExecutionContext


class NotifyOwnerPayload(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=1000)
    url: str | None = Field(default=None, max_length=500)


def register_builtin_actions(registry: ActionRegistry, *, push: PushService) -> None:
    async def notify_owner(ctx: ExecutionContext, payload: NotifyOwnerPayload) -> dict[str, Any]:
        result = await push.send(title=payload.title, body=payload.body, url=payload.url)
        if not result.configured:
            return {
                "delivered": 0,
                "note": "Push notifications are not set up (VAPID keys missing).",
            }
        return {"delivered": result.delivered, "expired_subscriptions_removed": result.removed}

    registry.register(
        ActionSpec(
            kind="notify.owner",
            payload_model=NotifyOwnerPayload,
            executor=notify_owner,
            summarize=lambda p: f"Notify you: {p.title}",
            timeout_seconds=30,
        )
    )
