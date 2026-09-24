"""Shared policy vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


class Autonomy(StrEnum):
    L0 = "L0"  # observe only
    L1 = "L1"  # draft only
    L2 = "L2"  # needs approval
    L3 = "L3"  # act, then notify


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Channel(StrEnum):
    PWA = "pwa"
    PWA_PASSKEY = "pwa_passkey"
    TELEGRAM = "telegram"
    VOICE = "voice"


class Status(StrEnum):
    REFUSED = "refused"
    DRAFT_ONLY = "draft_only"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"
    EXPIRED = "expired"


POLICY_CHANNEL = "policy"
"""`decided_via` value for proposals approved automatically at L3."""

Outcome = Literal["pass", "warn", "block"]


@dataclass(frozen=True)
class ValidationResult:
    validator: str
    outcome: Outcome
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"validator": self.validator, "outcome": self.outcome, "message": self.message}
