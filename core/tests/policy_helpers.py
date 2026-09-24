"""Shared builders for policy-engine tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.log import AuditLog
from jarvis.clock import FrozenClock
from jarvis.policy.config import PoliciesConfig, parse_policies
from jarvis.policy.engine import PolicyEngine
from jarvis.policy.registry import ActionRegistry, ActionSpec, ExecutionContext, OutcomeUnknownError
from jarvis.policy.state import StaticGate


class EchoPayload(BaseModel):
    text: str = Field(min_length=1)
    to: list[str] = []
    attachments: list[str] = []


@dataclass
class RecordingExecutor:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    mode: str = "ok"  # ok | error | unknown | hang

    async def __call__(self, ctx: ExecutionContext, payload: EchoPayload) -> dict[str, Any]:
        self.calls.append((str(ctx.proposal_id), payload.model_dump()))
        if self.mode == "error":
            raise RuntimeError("provider said no")
        if self.mode == "unknown":
            raise OutcomeUnknownError("connection dropped after send")
        if self.mode == "hang":
            await asyncio.sleep(10)
        return {"echo": payload.text}


def policies(**overrides: Any) -> PoliciesConfig:
    raw: dict[str, Any] = {
        "version": 1,
        "defaults": {
            "undo_window_seconds": 60,
            "timezone": "Africa/Nairobi",
            "quiet_hours": {"start": "21:30", "end": "07:00"},
        },
        "approval_channels": {
            "low": ["pwa", "pwa_passkey", "telegram", "voice"],
            "medium": ["pwa", "pwa_passkey", "telegram", "voice"],
            "high": ["pwa_passkey"],
            "critical": ["pwa_passkey"],
        },
        "action_kinds": {
            "test.observe": {"description": "L0", "autonomy": "L0", "risk": "low"},
            "test.draft": {"description": "L1", "autonomy": "L1", "risk": "low"},
            "test.ask": {"description": "L2", "autonomy": "L2", "risk": "medium"},
            "test.auto": {
                "description": "L3",
                "autonomy": "L3",
                "risk": "low",
                "undo_window_seconds": 0,
                "validators": ["secrets_scan"],
            },
            "test.risky": {"description": "high", "autonomy": "L2", "risk": "high"},
            "test.capped": {
                "description": "cap",
                "autonomy": "L3",
                "risk": "low",
                "undo_window_seconds": 0,
                "max_per_day": 2,
            },
            "test.quiet": {
                "description": "quiet",
                "autonomy": "L2",
                "risk": "medium",
                "defer_during_quiet_hours": True,
            },
            "test.mail": {
                "description": "mail-like",
                "autonomy": "L2",
                "risk": "medium",
                "validators": [
                    "recipients_known",
                    "attachment_mentioned",
                    "secrets_scan",
                    "links_safe",
                ],
            },
        },
    }
    raw.update(overrides)
    return parse_policies(raw)


class SetContacts:
    def __init__(self, known: set[str] | None = None) -> None:
        self.known = {k.lower() for k in (known or set())}

    async def is_known(self, session: AsyncSession, address: str) -> bool:
        return address.lower() in self.known


def build_engine(
    *,
    clock: FrozenClock,
    audit: AuditLog | None = None,
    gate_open: bool = True,
    executor: RecordingExecutor | None = None,
    config: PoliciesConfig | None = None,
    known_contacts: set[str] | None = None,
    timeout: float = 5.0,
) -> tuple[PolicyEngine, RecordingExecutor]:
    executor = executor or RecordingExecutor()
    config = config or policies()
    registry = ActionRegistry()
    for kind in config.action_kinds:
        registry.register(
            ActionSpec(
                kind=kind,
                payload_model=EchoPayload,
                executor=executor,
                summarize=lambda p: f"echo {p.text}",
                timeout_seconds=timeout,
            )
        )
    engine = PolicyEngine(
        config=config,
        registry=registry,
        audit=audit or AuditLog(clock),
        clock=clock,
        gate=StaticGate(open=gate_open, reason="profile not signed off"),
        contacts=SetContacts(known_contacts),
    )
    return engine, executor
