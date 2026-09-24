"""Start a throwaway Jarvis for browser end-to-end tests.

Test tooling only: it wipes the database named in DATABASE_URL, so it refuses
to run unless JARVIS_ENV=test and the database name ends in "_e2e".
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import sys
import threading
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response, UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# What the fake speech server "hears", whatever you say (the browser test plays
# tests/fixtures/audio/question.wav, which says exactly this).
HEARD = "What is on my calendar today?"


async def reset(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS dbos CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


def fake_speech_app() -> FastAPI:
    """A stand-in for the local speech server (speaches), speaking its API."""
    # (FastAPI reads these handlers' type hints, so its types are imported at the top.)
    from jarvis.config import REPO_ROOT
    from jarvis.voice.config import load_voice_config

    speech = load_voice_config(REPO_ROOT / "config" / "voice.yaml").speech
    app = FastAPI()

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"data": [{"id": speech.stt_model}, {"id": speech.tts_model}]}

    @app.post("/v1/audio/transcriptions")
    async def transcribe(file: UploadFile) -> dict[str, str]:
        await file.read()
        return {"text": HEARD}

    @app.post("/v1/audio/speech")
    async def speak(request: Request) -> Response:
        body = await request.json()
        # 0.4 s of a quiet 220 Hz tone per sentence, as 16-bit PCM at 24 kHz.
        samples = [int(3000 * math.sin(2 * math.pi * 220 * i / 24_000)) for i in range(9_600)]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        if body.get("response_format") == "pcm":
            return Response(pcm, media_type="audio/pcm")
        return Response(b"ID3" + pcm[:1000], media_type="audio/mpeg")

    return app


def start_fake_speech(port: int) -> None:
    import uvicorn

    config = uvicorn.Config(fake_speech_app(), host="127.0.0.1", port=port, log_level="warning")
    threading.Thread(target=uvicorn.Server(config).run, daemon=True).start()


def main() -> None:
    url = os.environ["DATABASE_URL"]
    if os.environ.get("JARVIS_ENV") != "test" or not urlparse(url).path.endswith("_e2e"):
        sys.exit("refusing to reset: needs JARVIS_ENV=test and a database ending in _e2e")
    asyncio.run(reset(url))
    if port := os.environ.get("E2E_SPEECH_PORT"):
        start_fake_speech(int(port))
    from jarvis.cli import main as cli

    sys.exit(cli(["serve"]))


if __name__ == "__main__":
    main()
