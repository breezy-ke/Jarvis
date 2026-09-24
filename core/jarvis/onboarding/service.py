"""The onboarding interview: one resumable conversation per module.

The interviewer agent can only write to the profile fields of its own module,
and every write is validated against the profile schema. Transcripts are
kept, so a module can be paused and resumed, by text or later by voice.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from sqlalchemy import select

from jarvis.chat.service import ChatEvent
from jarvis.db.models import OnboardingModuleState
from jarvis.db.session import transaction
from jarvis.llm.router import RouterError, StreamDone, TextDelta
from jarvis.onboarding.modules import MODULES, MODULES_BY_ID, OnboardingModule
from jarvis.profile.schema import PathError, get_path, is_filled
from jarvis.profile.service import completeness
from jarvis.services import Services

INTERVIEW_STYLE = """\
You are Jarvis, onboarding your new owner. You are a warm, sharp interviewer.

How to interview:
- Ask ONE question at a time. Keep each message under 60 words.
- Adapt: skip what the owner already answered, and dig deeper where answers are vague.
- Save answers as soon as you have them with `save_answer`. Never invent or embellish
  answers: record only what the owner said, or a direct inference you confirm with them.
- When this module's fields are covered (or the owner declines the rest), summarise what
  you learned in two or three lines, ask them to confirm it, then call `finish_module`.
- If the owner wants to stop, reassure them that progress is saved.
"""


@dataclass
class OnboardingDeps:
    services: Services
    module: OnboardingModule


@dataclass(frozen=True)
class ModuleStatus:
    id: str
    title: str
    status: str
    optional: bool
    filled: int
    total: int
    required_missing: list[str]
    summary: str | None


def build_onboarding_agent() -> Agent[OnboardingDeps, str]:
    agent: Agent[OnboardingDeps, str] = Agent(
        deps_type=OnboardingDeps, instructions=INTERVIEW_STYLE, name="onboarding", retries=2
    )

    @agent.tool
    async def save_answer(ctx: RunContext[OnboardingDeps], field: str, value_json: str) -> str:
        """Save the owner's answer to one profile field of this module.

        Args:
            field: The profile field, exactly as listed for this module (e.g. "business.services").
            value_json: The value as JSON: a string '"Nairobi, Kenya"', a list
                '["Next.js", "Laravel"]', or a list of objects for structured fields
                such as rate_card or contacts.
        """
        module = ctx.deps.module
        if field not in module.fields:
            raise ModelRetry(
                f"'{field}' is not part of this module. Use one of: {', '.join(module.fields)}"
            )
        try:
            value: Any = json.loads(value_json)
        except json.JSONDecodeError:
            value = value_json  # a bare string is fine for text fields
        services = ctx.deps.services
        try:
            async with transaction(services.session_factory) as session:
                snapshot = await services.profiles.current(session)
                current = get_path(snapshot.data, field)
                if isinstance(current, list) and not isinstance(value, list):
                    value = [value]
                await services.profiles.update(
                    session, {field: value}, created_by=f"onboarding:{module.id}", note="interview"
                )
        except PathError as exc:
            raise ModelRetry(f"That value doesn't fit {field}: {exc}") from exc
        return f"Saved {field}."

    @agent.tool
    async def finish_module(ctx: RunContext[OnboardingDeps], summary: str) -> str:
        """Mark this module done after the owner confirmed your summary.

        Args:
            summary: Two or three lines on what you learned in this module.
        """
        services = ctx.deps.services
        module = ctx.deps.module
        async with transaction(services.session_factory) as session:
            snapshot = await services.profiles.current(session)
            missing = [f for f in module.required if not is_filled(get_path(snapshot.data, f))]
            state = await session.get(OnboardingModuleState, module.id, with_for_update=True)
            assert state is not None
            state.status = "completed"
            state.summary = summary
            state.completed_at = services.clock.now()
            state.updated_at = services.clock.now()
            await services.audit.append(
                session,
                actor=f"onboarding:{module.id}",
                event_type="onboarding.module_completed",
                subject_type="onboarding",
                subject_id=module.id,
                summary=f"Finished onboarding module: {module.title}",
                data={"module": module.id, "required_missing": missing},
            )
        if missing:
            return (
                "Module closed, but these required fields are still empty: "
                + ", ".join(missing)
                + ". Tell the owner they can fill them later on the profile page."
            )
        return "Module complete."

    return agent


def _module_context(module: OnboardingModule, profile_data: dict[str, Any]) -> str:
    current = {f: get_path(profile_data, f) for f in module.fields}
    required = ", ".join(module.required) or "(none: optional module)"
    return (
        f"## Current module: {module.title}\n"
        f"Goal: {module.goal}\n"
        f"Fields you may save: {', '.join(module.fields)}\n"
        f"Required for the autonomy gate: {required}\n"
        f"Interview guidance: {module.guidance}\n"
        f"Suggested opening question: {module.opening}\n\n"
        f"## What is already known for this module (JSON)\n{json.dumps(current, indent=1)}"
    )


def _transcript_to_messages(transcript: list[dict[str, str]]) -> list[ModelMessage]:
    messages: list[ModelMessage] = []
    for entry in transcript[-30:]:
        if entry["role"] == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(entry["content"])]))
        else:
            messages.append(ModelResponse(parts=[TextPart(entry["content"])]))
    return messages


class OnboardingService:
    def __init__(
        self, services: Services, *, agent: Agent[OnboardingDeps, str] | None = None
    ) -> None:
        self._s = services
        self.agent = agent or build_onboarding_agent()

    async def _state(self, module_id: str) -> OnboardingModuleState:
        async with transaction(self._s.session_factory) as session:
            state = await session.get(OnboardingModuleState, module_id, with_for_update=True)
            if state is None:
                state = OnboardingModuleState(
                    module_id=module_id,
                    status="not_started",
                    transcript=[],
                    updated_at=self._s.clock.now(),
                )
                session.add(state)
            return state

    async def statuses(self) -> list[ModuleStatus]:
        async with self._s.session_factory() as session:
            snapshot = await self._s.profiles.current(session)
            states = {s.module_id: s for s in await session.scalars(select(OnboardingModuleState))}
        data = snapshot.data
        result: list[ModuleStatus] = []
        for module in MODULES:
            state = states.get(module.id)
            filled = sum(1 for f in module.fields if is_filled(get_path(data, f)))
            result.append(
                ModuleStatus(
                    id=module.id,
                    title=module.title,
                    status=state.status if state else "not_started",
                    optional=module.optional,
                    filled=filled,
                    total=len(module.fields),
                    required_missing=[
                        f for f in module.required if not is_filled(get_path(data, f))
                    ],
                    summary=state.summary if state else None,
                )
            )
        return result

    async def overview(self) -> dict[str, Any]:
        statuses = await self.statuses()
        async with self._s.session_factory() as session:
            snapshot = await self._s.profiles.current(session)
            gate = await self._s.profiles.status(session)
        score = completeness(snapshot.profile)
        return {
            "completeness": round(score.score, 3),
            "missing_required": score.missing,
            "modules_completed": sum(1 for s in statuses if s.status == "completed"),
            "modules_total": len(statuses),
            "gate_open": gate.open,
            "gate_reason": gate.reason,
            "signed_off": snapshot.ever_signed_off,
        }

    async def transcript(self, module_id: str) -> list[dict[str, str]]:
        if module_id not in MODULES_BY_ID:
            raise KeyError(module_id)
        state = await self._state(module_id)
        return list(state.transcript)

    async def skip(self, module_id: str) -> None:
        if module_id not in MODULES_BY_ID:
            raise KeyError(module_id)
        await self._state(module_id)
        async with transaction(self._s.session_factory) as session:
            state = await session.get(OnboardingModuleState, module_id, with_for_update=True)
            assert state is not None
            state.status = "skipped"
            state.updated_at = self._s.clock.now()

    async def stream_turn(self, module_id: str, text: str | None) -> AsyncIterator[ChatEvent]:
        module = MODULES_BY_ID.get(module_id)
        if module is None:
            raise KeyError(module_id)
        state = await self._state(module_id)
        transcript: list[dict[str, str]] = list(state.transcript)
        if text is None and transcript:
            yield ChatEvent("done", {"text": transcript[-1]["content"], "module": module_id})
            return
        prompt = (
            text.strip()
            if text
            else "(The owner opened this module. Greet briefly and ask your first question.)"
        )

        async with self._s.session_factory() as session:
            snapshot = await self._s.profiles.current(session)
        context = _module_context(module, snapshot.data)
        deps = OnboardingDeps(services=self._s, module=module)
        chunks: list[str] = []
        yield ChatEvent("start", {"module": module_id})
        try:
            async for event in self._s.router.stream(
                self.agent,
                prompt,
                task="onboarding",
                deps=deps,
                message_history=_transcript_to_messages(transcript),
                instructions=context,
            ):
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                    yield ChatEvent("delta", {"text": event.text})
                elif isinstance(event, StreamDone):
                    reply = event.output or "".join(chunks)
                    if text:
                        transcript.append({"role": "user", "content": text.strip()})
                    transcript.append({"role": "assistant", "content": reply})
                    await self._save_transcript(module_id, transcript)
                    yield ChatEvent(
                        "done", {"text": reply, "module": module_id, "model": event.model_ref}
                    )
        except RouterError as exc:
            yield ChatEvent("error", {"message": str(exc)})

    async def _save_transcript(self, module_id: str, transcript: list[dict[str, str]]) -> None:
        async with transaction(self._s.session_factory) as session:
            state = await session.get(OnboardingModuleState, module_id, with_for_update=True)
            assert state is not None
            state.transcript = transcript
            if state.status == "not_started" or state.status == "skipped":
                state.status = "in_progress"
            state.updated_at = self._s.clock.now()
