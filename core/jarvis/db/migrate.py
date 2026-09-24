"""Run Alembic migrations programmatically (at startup and from the CLI)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

CORE_DIR = Path(__file__).resolve().parents[2]
_MIGRATION_LOCK = 0x4D494752  # "MIGR"


def _config(connection: Connection) -> Config:
    cfg = Config(str(CORE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(CORE_DIR / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


def _upgrade(connection: Connection) -> None:
    # Serialise concurrent starts (for example two containers) with an advisory lock.
    connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK})
    try:
        command.upgrade(_config(connection), "head")
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK})


async def run_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
