"""Registry of implemented action kinds: payload schema, summary and executor."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from jarvis.clock import Clock
from jarvis.db.session import SessionFactory


class OutcomeUnknownError(Exception):
    """Raise from an executor when the side effect may or may not have happened.

    Example: the connection dropped after an email was handed to the provider.
    Jarvis then marks the action `unknown_outcome` and never retries it on its own.
    """


@dataclass(frozen=True)
class ExecutionContext:
    proposal_id: uuid.UUID
    kind: str
    session_factory: SessionFactory
    clock: Clock


Executor = Callable[[ExecutionContext, Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ActionSpec:
    kind: str
    payload_model: type[BaseModel]
    executor: Executor
    summarize: Callable[[Any], str]
    timeout_seconds: float = 60.0


class ActionRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        if spec.kind in self._specs:
            raise ValueError(f"action kind {spec.kind!r} registered twice")
        self._specs[spec.kind] = spec

    def get(self, kind: str) -> ActionSpec | None:
        return self._specs.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._specs)
