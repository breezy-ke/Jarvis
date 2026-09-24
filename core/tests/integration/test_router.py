from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from jarvis.audit.log import AuditLog
from jarvis.clock import FrozenClock
from jarvis.config import REPO_ROOT
from jarvis.db.models import AuditEvent, LLMCall
from jarvis.db.session import SessionFactory, transaction
from jarvis.llm.config import ModelsConfig, PrivacyClass, load_models_config, parse_models_config
from jarvis.llm.privacy import allowed
from jarvis.llm.router import (
    ModelRouter,
    NoAvailableModel,
    NoCompliantModel,
    StreamDone,
    StreamInterrupted,
    TextDelta,
)

pytestmark = pytest.mark.db


def config(**overrides: Any) -> ModelsConfig:
    raw: dict[str, Any] = {
        "version": 1,
        "budget": {"monthly_usd_cap": 0},
        "providers": {
            "local": {"kind": "fake", "local": True, "trains_on_data": False},
            "private": {
                "kind": "fake",
                "trains_on_data": False,
                "zero_data_retention": True,
            },
            "keyed": {
                "kind": "groq",
                "api_key_env": "TEST_GROQ_KEY",
                "trains_on_data": False,
                "zero_data_retention": True,
            },
            "public": {"kind": "fake", "trains_on_data": True},
            "paid": {"kind": "fake", "paid": True, "trains_on_data": False},
        },
        "models": {
            "local-m": {"provider": "local", "model": "l"},
            "private-m": {"provider": "private", "model": "p", "limits": {"rpm": 3}},
            "keyed-m": {"provider": "keyed", "model": "k"},
            "public-m": {"provider": "public", "model": "pub"},
            "paid-m": {"provider": "paid", "model": "$", "usd_per_mtok_in": 1_000_000},
        },
        "tasks": {
            "chat": {"privacy": "personal", "candidates": ["private-m", "local-m"]},
            "public_only": {"privacy": "personal", "candidates": ["public-m"]},
            "summarize": {"privacy": "public", "candidates": ["public-m", "private-m"]},
            "secret": {"privacy": "confidential", "candidates": ["public-m", "private-m"]},
            "keyed": {"privacy": "personal", "candidates": ["keyed-m"]},
            "paid": {"privacy": "personal", "candidates": ["paid-m"]},
        },
    }
    raw.update(overrides)
    return parse_models_config(raw)


def make_router(
    session_factory: SessionFactory, clock: FrozenClock, cfg: ModelsConfig | None = None
) -> ModelRouter:
    return ModelRouter(
        cfg or config(),
        env={},
        session_factory=session_factory,
        clock=clock,
        audit=AuditLog(clock),
        allow_fake=True,
    )


def failing_model(status: int = 500) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=status, model_name="broken")

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        raise ModelHTTPError(status_code=status, model_name="broken")
        yield ""  # pragma: no cover

    return FunctionModel(fn, stream_function=stream)


def text_model(text: str) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        for word in text.split():
            yield word + " "

    return FunctionModel(fn, stream_function=stream)


# --- Privacy: fail closed ---------------------------------------------------------------


def test_training_providers_never_see_personal_data(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    with pytest.raises(NoCompliantModel, match="may train on your data"):
        router.compliant_candidates("public_only")


def test_callers_can_only_tighten_privacy(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    public = [c.ref for c in router.compliant_candidates("summarize")]
    assert public == ["public-m", "private-m"]
    tightened = [c.ref for c in router.compliant_candidates("summarize", PrivacyClass.PERSONAL)]
    assert tightened == ["private-m"]
    assert router.effective_privacy("chat", PrivacyClass.PUBLIC) == PrivacyClass.PERSONAL


def test_confidential_requires_zero_retention(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    assert [c.ref for c in router.compliant_candidates("secret")] == ["private-m"]


def test_shipped_models_config_respects_privacy() -> None:
    """The privacy-routing eval: every shipped task, 100% compliant."""
    cfg = load_models_config(REPO_ROOT / "config" / "models.yaml")
    for name, task in cfg.tasks.items():
        for ref in task.candidates:
            provider = cfg.providers[cfg.models[ref].provider]
            assert allowed(provider, task.privacy), f"{name} -> {ref} violates {task.privacy}"
    gemini = cfg.providers["gemini"]
    assert not allowed(gemini, PrivacyClass.PERSONAL)
    assert not allowed(cfg.providers["openrouter"], PrivacyClass.PERSONAL)


# --- Availability ---------------------------------------------------------------------------


async def test_missing_api_key_is_explained(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    with pytest.raises(NoAvailableModel, match="TEST_GROQ_KEY is not set"):
        await router.usable("keyed")


async def test_paid_providers_follow_the_budget(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    with pytest.raises(NoAvailableModel, match="budget cap is 0"):
        await router.usable("paid")
    funded = make_router(session_factory, clock, config(budget={"monthly_usd_cap": 5}))
    funded._models["paid-m"] = text_model("hi")
    agent: Agent[None, str] = Agent()
    await funded.run(agent, "hello there friend", task="paid")  # costs > $5 at this price
    with pytest.raises(NoAvailableModel, match=r"budget of \$5\.00 used up"):
        await funded.usable("paid")


async def test_quota_headroom_skips_a_busy_model(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    async with transaction(session_factory) as session:
        for _ in range(2):
            session.add(
                LLMCall(
                    ts=clock.now(),
                    task="chat",
                    privacy="personal",
                    model_ref="private-m",
                    provider="private",
                    model="p",
                    status="ok",
                )
            )
    usable = [c.ref for c in await router.usable("chat")]
    assert usable == ["local-m"]  # rpm 3 * 0.9 headroom = 2.7 < 3 projected
    clock.advance(minutes=2)
    assert [c.ref for c in await router.usable("chat")] == ["private-m", "local-m"]


# --- Fallback -----------------------------------------------------------------------------


async def test_run_falls_back_and_records_everything(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    router._models["private-m"] = failing_model(503)
    router._models["local-m"] = text_model("from local")
    agent: Agent[None, str] = Agent()
    outcome = await router.run(agent, "hi", task="chat")
    assert outcome.output == "from local"
    assert outcome.model_ref == "local-m"
    async with session_factory() as session:
        calls = [
            (c.model_ref, c.status)
            for c in await session.scalars(select(LLMCall).order_by(LLMCall.id))
        ]
        events = list(await session.scalars(select(AuditEvent.event_type)))
        assert (await AuditLog(clock).verify(session)).ok
    assert calls == [("private-m", "error"), ("local-m", "ok")]
    assert events == ["llm.call", "llm.call"]
    # The failed model cools down, so the next call goes straight to the fallback.
    assert [c.ref for c in await router.usable("chat")] == ["local-m"]
    clock.advance(seconds=31)
    assert [c.ref for c in await router.usable("chat")] == ["private-m", "local-m"]


async def test_all_models_failing_is_an_error(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    router._models["private-m"] = failing_model()
    router._models["local-m"] = failing_model(429)
    with pytest.raises(NoAvailableModel, match="All models failed"):
        await router.run(Agent(), "hi", task="chat")


async def test_rate_limit_cools_down_longer(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    router._models["private-m"] = failing_model(429)
    router._models["local-m"] = text_model("ok")
    await router.run(Agent(), "hi", task="chat")
    clock.advance(seconds=31)
    assert [c.ref for c in await router.usable("chat")] == ["local-m"]
    clock.advance(seconds=30)
    assert [c.ref for c in await router.usable("chat")] == ["private-m", "local-m"]


async def test_stream_falls_back_before_the_first_token(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    router._models["private-m"] = failing_model()
    router._models["local-m"] = text_model("streamed reply here")
    events = [e async for e in router.stream(Agent(), "hi", task="chat")]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    done = events[-1]
    assert text.strip() == "streamed reply here"
    assert isinstance(done, StreamDone)
    assert done.model_ref == "local-m"


async def test_stream_interrupted_mid_answer_is_not_silently_retried(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    async def half(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield "partial "
        raise ModelHTTPError(status_code=500, model_name="flaky")

    router = make_router(session_factory, clock)
    router._models["private-m"] = FunctionModel(stream_function=half)
    router._models["local-m"] = text_model("never used")
    seen: list[str] = []
    with pytest.raises(StreamInterrupted):
        async for event in router.stream(Agent(), "hi", task="chat"):
            if isinstance(event, TextDelta):
                seen.append(event.text)
    assert seen == ["partial "]


async def test_fake_model_can_call_tools(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    router = make_router(session_factory, clock)
    agent: Agent[None, str] = Agent()
    calls: list[str] = []

    @agent.tool_plain
    def lookup(topic: str) -> str:
        calls.append(topic)
        return f"facts about {topic}"

    outcome = await router.run(agent, '/tool lookup {"topic": "pricing"}', task="chat")
    assert calls == ["pricing"]
    assert "facts about pricing" in outcome.output
    events = [e async for e in router.stream(agent, '/tool lookup {"topic": "x"}', task="chat")]
    assert isinstance(events[-1], StreamDone)
    assert calls == ["pricing", "x"]


def test_unknown_references_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown model"):
        config(tasks={"chat": {"privacy": "public", "candidates": ["ghost"]}})


async def test_cooldown_is_per_model(session_factory: SessionFactory, clock: FrozenClock) -> None:
    router = make_router(session_factory, clock)
    router._cooldown_until["local-m"] = clock.now() + timedelta(minutes=5)
    assert [c.ref for c in await router.usable("chat")] == ["private-m"]
