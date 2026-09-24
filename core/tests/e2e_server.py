"""Start a throwaway Jarvis for browser end-to-end tests.

Test tooling only: it wipes the database named in DATABASE_URL, so it refuses
to run unless JARVIS_ENV=test and the database name ends in "_e2e".
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def reset(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS dbos CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


def main() -> None:
    url = os.environ["DATABASE_URL"]
    if os.environ.get("JARVIS_ENV") != "test" or not urlparse(url).path.endswith("_e2e"):
        sys.exit("refusing to reset: needs JARVIS_ENV=test and a database ending in _e2e")
    asyncio.run(reset(url))
    from jarvis.cli import main as cli

    sys.exit(cli(["serve"]))


if __name__ == "__main__":
    main()
