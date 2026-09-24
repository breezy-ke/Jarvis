"""The model router: privacy first, then availability, then fallback.

For every call:
  1. Work out the data class: the task's own class, or a stricter one the
     caller passes. It can never be loosened.
  2. Keep only the candidates allowed to see that class. If none are left,
     raise `NoCompliantModel`. It never quietly falls back to a less private model.
  3. Skip candidates that have no API key, are over budget, are cooling down
     after errors, or are near a free-tier quota.
  4. Try the rest in order. Fall back only on transport or provider errors,
     and for streams only before the first token has been shown.
  5. Record every attempt in `llm_calls` and the audit log. Record metadata
     only, never the content.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from jarvis.audit.log import AuditLog
from jarvis.clock import Clock
from jarvis.db.models import LLMCall
from jarvis.db.session import SessionFactory, transaction
from jarvis.llm.config import (
    ModelConfig,
    ModelsConfig,
    PrivacyClass,
    ProviderConfig,
    ProviderKind,
)
from jarvis.llm.privacy import allowed, explain
from jarvis.llm.providers import build_model, provider_ready
from jarvis.llm.quota import month_spend, quota_blocker

FALLBACK_ERRORS: tuple[type[BaseException], ...] = (
    ModelAPIError,
    UnexpectedModelBehavior,
    httpx.HTTPError,
    TimeoutError,
    ConnectionError,
    OSError,
)
DEFAULT_LIMITS = UsageLimits(request_limit=12, total_tokens_limit=200_000)


class RouterError(RuntimeError):
    pass


class NoCompliantModel(RouterError):
    """No configured model may see this class of data. Fails closed."""


class NoAvailableModel(RouterError):
    """Compliant models exist but none can take the call right now."""


class StreamInterrupted(RouterError):
    """The model failed after text had already been streamed to the owner."""


@dataclass(frozen=True)
class Candidate:
    ref: str
    model: ModelConfig
    provider_name: str
    provider: ProviderConfig


@dataclass(frozen=True)
class CandidateStatus:
    candidate: Candidate
    available: bool
    reason: str


@dataclass
class RunOutcome:
    output: Any
    model_ref: str
    usage: RunUsage
    new_messages: list[ModelMessage]


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class StreamDone:
    output: str
    model_ref: str
    usage: RunUsage
    new_messages: list[ModelMessage]


def model_settings_for(cand: Candidate) -> ModelSettings | None:
    """Per-model request settings from models.yaml (output cap, reasoning effort)."""
    settings: dict[str, Any] = {}
    if cand.model.max_tokens is not None:
        settings["max_tokens"] = cand.model.max_tokens
    if cand.model.reasoning is not None and cand.provider.kind != ProviderKind.FAKE:
        settings["openai_reasoning_effort"] = cand.model.reasoning
    return cast(ModelSettings, settings) if settings else None


class ModelRouter:
    def __init__(
        self,
        config: ModelsConfig,
        *,
        env: Mapping[str, str],
        session_factory: SessionFactory,
        clock: Clock,
        audit: AuditLog | None = None,
        allow_fake: bool = False,
    ) -> None:
        self.config = config
        self._env = env
        self._sf = session_factory
        self._clock = clock
        self._audit = audit or AuditLog(clock)
        self._allow_fake = allow_fake
        self._models: dict[str, Model] = {}
        self._cooldown_until: dict[str, datetime] = {}

    # --- Selection ------------------------------------------------------------------

    def effective_privacy(self, task: str, privacy: PrivacyClass | None) -> PrivacyClass:
        task_cfg = self.config.tasks.get(task)
        if task_cfg is None:
            raise RouterError(f"unknown task {task!r}")
        return task_cfg.privacy.stricter(privacy)

    def compliant_candidates(
        self, task: str, privacy: PrivacyClass | None = None
    ) -> list[Candidate]:
        effective = self.effective_privacy(task, privacy)
        candidates: list[Candidate] = []
        refused: list[str] = []
        for ref in self.config.tasks[task].candidates:
            model = self.config.models[ref]
            provider = self.config.providers[model.provider]
            if allowed(provider, effective):
                candidates.append(Candidate(ref, model, model.provider, provider))
            else:
                refused.append(explain(model.provider, provider, effective))
        if not candidates:
            raise NoCompliantModel(
                f"No configured model may see {effective.value} data for '{task}'. "
                + "; ".join(refused)
            )
        return candidates

    async def availability(
        self, task: str, privacy: PrivacyClass | None = None, *, est_tokens: int = 2_000
    ) -> list[CandidateStatus]:
        candidates = self.compliant_candidates(task, privacy)
        now = self._clock.now()
        paid = [name for name, p in self.config.providers.items() if p.paid]
        statuses: list[CandidateStatus] = []
        async with self._sf() as session:
            spend = await month_spend(session, paid, now) if paid else 0.0
            for cand in candidates:
                reason = provider_ready(cand.provider, self._env, allow_fake=self._allow_fake)
                if reason is None and cand.provider.paid:
                    cap = self.config.budget.monthly_usd_cap
                    if cap <= 0:
                        reason = "paid providers are off (budget cap is 0)"
                    elif spend >= cap:
                        reason = f"monthly budget of ${cap:.2f} used up"
                if reason is None and (until := self._cooldown_until.get(cand.ref)) and until > now:
                    reason = f"cooling down after errors until {until:%H:%M:%S} UTC"
                if reason is None:
                    reason = await quota_blocker(
                        session,
                        model_ref=cand.ref,
                        limits=cand.model.limits,
                        now=now,
                        est_tokens=est_tokens,
                        headroom=self.config.quota_headroom,
                    )
                statuses.append(CandidateStatus(cand, reason is None, reason or "available"))
        return statuses

    async def usable(
        self, task: str, privacy: PrivacyClass | None = None, *, est_tokens: int = 2_000
    ) -> list[Candidate]:
        statuses = await self.availability(task, privacy, est_tokens=est_tokens)
        usable = [s.candidate for s in statuses if s.available]
        if not usable:
            reasons = "; ".join(f"{s.candidate.ref}: {s.reason}" for s in statuses)
            raise NoAvailableModel(f"No model can take '{task}' right now ({reasons}).")
        return usable

    def model_for(self, cand: Candidate) -> Model:
        if cand.ref not in self._models:
            self._models[cand.ref] = build_model(
                cand.ref, cand.model, cand.provider, self._env, allow_fake=self._allow_fake
            )
        return self._models[cand.ref]

    # --- Execution ------------------------------------------------------------------

    async def run(
        self,
        agent: Agent[Any, Any],
        prompt: str | None,
        *,
        task: str,
        privacy: PrivacyClass | None = None,
        deps: Any = None,
        message_history: Sequence[ModelMessage] | None = None,
        usage_limits: UsageLimits | None = None,
        instructions: str | None = None,
    ) -> RunOutcome:
        effective = self.effective_privacy(task, privacy)
        est = _estimate_tokens(prompt, message_history)
        errors: list[str] = []
        for cand in await self.usable(task, effective, est_tokens=est):
            started = time.monotonic()
            try:
                result = await agent.run(
                    prompt,
                    model=self.model_for(cand),
                    deps=deps,
                    message_history=list(message_history or []),
                    usage_limits=usage_limits or DEFAULT_LIMITS,
                    instructions=instructions,
                    model_settings=model_settings_for(cand),
                )
            except FALLBACK_ERRORS as exc:
                await self._record_failure(cand, task, effective, exc, started)
                errors.append(f"{cand.ref}: {_short(exc)}")
                continue
            await self._record(cand, task, effective, "ok", result.usage, started)
            return RunOutcome(
                output=result.output,
                model_ref=cand.ref,
                usage=result.usage,
                new_messages=list(result.new_messages()),
            )
        raise NoAvailableModel("All models failed: " + "; ".join(errors))

    async def stream(
        self,
        agent: Agent[Any, str],
        prompt: str,
        *,
        task: str,
        privacy: PrivacyClass | None = None,
        deps: Any = None,
        message_history: Sequence[ModelMessage] | None = None,
        usage_limits: UsageLimits | None = None,
        instructions: str | None = None,
    ) -> AsyncIterator[TextDelta | StreamDone]:
        effective = self.effective_privacy(task, privacy)
        est = _estimate_tokens(prompt, message_history)
        errors: list[str] = []
        for cand in await self.usable(task, effective, est_tokens=est):
            started = time.monotonic()
            emitted = False
            try:
                async with agent.run_stream(
                    prompt,
                    model=self.model_for(cand),
                    deps=deps,
                    message_history=list(message_history or []),
                    usage_limits=usage_limits or DEFAULT_LIMITS,
                    instructions=instructions,
                    model_settings=model_settings_for(cand),
                ) as result:
                    async for delta in result.stream_text(delta=True, debounce_by=None):
                        if delta:
                            emitted = True
                            yield TextDelta(delta)
                    output = await result.get_output()
                    usage = result.usage
                    new_messages = list(result.new_messages())
            except FALLBACK_ERRORS as exc:
                await self._record_failure(cand, task, effective, exc, started)
                if emitted:
                    raise StreamInterrupted(
                        f"{cand.ref} stopped mid-answer: {_short(exc)}"
                    ) from exc
                errors.append(f"{cand.ref}: {_short(exc)}")
                continue
            await self._record(cand, task, effective, "ok", usage, started)
            yield StreamDone(
                output=str(output), model_ref=cand.ref, usage=usage, new_messages=new_messages
            )
            return
        raise NoAvailableModel("All models failed: " + "; ".join(errors))

    # --- Bookkeeping ------------------------------------------------------------------

    def _cool_down(self, cand: Candidate, exc: BaseException) -> None:
        seconds = 30
        if isinstance(exc, ModelHTTPError) and exc.status_code == 429:
            seconds = 60
        self._cooldown_until[cand.ref] = self._clock.now() + timedelta(seconds=seconds)

    async def _record_failure(
        self,
        cand: Candidate,
        task: str,
        privacy: PrivacyClass,
        exc: BaseException,
        started: float,
    ) -> None:
        self._cool_down(cand, exc)
        await self._record(cand, task, privacy, "error", None, started, error=_short(exc))

    async def _record(
        self,
        cand: Candidate,
        task: str,
        privacy: PrivacyClass,
        status: str,
        usage: RunUsage | None,
        started: float,
        *,
        error: str | None = None,
    ) -> None:
        latency_ms = int((time.monotonic() - started) * 1000)
        input_tokens = usage.input_tokens if usage else 0
        output_tokens = usage.output_tokens if usage else 0
        requests = max(usage.requests, 1) if usage else 1
        cost = (
            input_tokens * cand.model.usd_per_mtok_in + output_tokens * cand.model.usd_per_mtok_out
        ) / 1_000_000
        async with transaction(self._sf) as session:
            session.add(
                LLMCall(
                    ts=self._clock.now(),
                    task=task,
                    privacy=privacy.value,
                    model_ref=cand.ref,
                    provider=cand.provider_name,
                    model=cand.model.model,
                    status=status,
                    error=error,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    requests=requests,
                    latency_ms=latency_ms,
                    cost_usd=cost,
                )
            )
            await self._audit.append(
                session,
                actor="router",
                event_type="llm.call",
                summary=f"{task} via {cand.ref}: {status}",
                data={
                    "task": task,
                    "privacy": privacy.value,
                    "model_ref": cand.ref,
                    "provider": cand.provider_name,
                    "status": status,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "latency_ms": latency_ms,
                },
            )


def _short(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:300]


def _estimate_tokens(prompt: str | None, history: Sequence[ModelMessage] | None) -> int:
    chars = len(prompt or "")
    for message in history or []:
        for part in message.parts:
            content = getattr(part, "content", "")
            chars += len(content) if isinstance(content, str) else 200
    return chars // 4 + 1_500  # plus instructions and tool schemas
