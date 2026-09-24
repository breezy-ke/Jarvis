"""The database migrations build exactly the schema the models describe, both ways."""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from jarvis.config import REPO_ROOT
from jarvis.db.base import Base

pytestmark = pytest.mark.db


def _config(connection: Connection) -> Config:
    cfg = Config(str(REPO_ROOT / "core" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "core" / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


def _differences(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


async def test_the_migrations_match_the_models(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert await conn.run_sync(_differences) == []


async def test_every_migration_can_be_undone_and_redone(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: command.downgrade(_config(c), "base"))
        await conn.run_sync(lambda c: command.upgrade(_config(c), "head"))
    async with engine.connect() as conn:
        assert await conn.run_sync(_differences) == []
