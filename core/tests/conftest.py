"""Shared test fixtures.

Tests that touch the database use a real Postgres with pgvector, never SQLite,
so what we test is what runs in production. Point JARVIS_TEST_DATABASE_URL at
a disposable database. Every test starts from empty tables.
"""

from __future__ import annotations

import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
os.environ["JARVIS_ENV"] = "test"
# Tests must never reach a real service with a real credential.
for _var in (
    "GITHUB_TOKEN",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "VAPID_PUBLIC_KEY",
    "VAPID_PRIVATE_KEY",
    "JARVIS_SECRET_KEY",
    "DATABASE_URL",
    "PHOENIX_COLLECTOR_ENDPOINT",
    "OLLAMA_BASE_URL",
):
    os.environ.pop(_var, None)

from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from jarvis.audit.log import AuditLog
from jarvis.clock import FrozenClock
from jarvis.config import REPO_ROOT, Settings
from jarvis.db.base import Base
from jarvis.db.session import SessionFactory, create_session_factory
from jarvis.llm.config import ModelsConfig, parse_models_config
from jarvis.memory.embeddings import HashEmbedder
from jarvis.security.crypto import Vault
from jarvis.services import Services, build_services

TEST_DATABASE_URL = os.environ.get(
    "JARVIS_TEST_DATABASE_URL", "postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis_test"
)


def _run_migrations(connection: object) -> None:
    cfg = Config(str(REPO_ROOT / "core" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "core" / "alembic"))
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DATABASE_URL, pool_size=5, max_overflow=5)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("DROP SCHEMA IF EXISTS dbos CASCADE"))
        await conn.run_sync(_run_migrations)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> SessionFactory:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return create_session_factory(engine)


@pytest.fixture
async def session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def audit(clock: FrozenClock) -> AuditLog:
    return AuditLog(clock)


FAKE_TASKS = (
    "chat",
    "voice",
    "onboarding",
    "extraction",
    "ingestion",
    "triage",
    "drafting",
    "confidential",
    "public_summarize",
)


def fake_models_config() -> ModelsConfig:
    """Every task routed to the offline fake model."""
    return parse_models_config(
        {
            "version": 1,
            "providers": {"fake": {"kind": "fake", "local": True, "trains_on_data": False}},
            "models": {"fake-local": {"provider": "fake", "model": "fake"}},
            "tasks": {t: {"privacy": "personal", "candidates": ["fake-local"]} for t in FAKE_TASKS},
        }
    )


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "JARVIS_ENV": "test",
        "JARVIS_ALLOW_FAKE_LLM": True,
        "JARVIS_EMBEDDER": "hash",
        "DATABASE_URL": TEST_DATABASE_URL,
        "JARVIS_SECRET_KEY": Vault.generate_key(),
        "JARVIS_PUBLIC_ORIGIN": "http://localhost:8080",
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
def services(session_factory: SessionFactory, clock: FrozenClock) -> Services:
    return build_services(
        make_settings(),
        session_factory,
        clock=clock,
        embedder=HashEmbedder(),
        models_config=fake_models_config(),
    )
