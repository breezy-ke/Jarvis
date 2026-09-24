"""Voice: talk to Jarvis and hear it answer (Pipecat pipeline, speech server client)."""

from __future__ import annotations

import logging
from typing import Any

PIPECAT_LOG_LEVEL = "WARNING"


def _to_python_logging(message: Any) -> None:
    record = message.record
    logging.getLogger(f"pipecat.{record['name']}").log(record["level"].no, record["message"])


def route_pipecat_logs() -> None:
    """Pipecat logs through loguru at debug level, and those lines include what was
    said ("Generating TTS [...]"). Keep only its warnings and errors, and send them
    through Python logging with the rest of Jarvis's logs."""
    from loguru import logger

    logger.remove()
    logger.add(_to_python_logging, level=PIPECAT_LOG_LEVEL, format="{message}")


# Runs before any submodule imports Pipecat (and before its start-up banner).
route_pipecat_logs()
