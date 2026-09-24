"""Telegram API: link your Telegram account, check the bot, unlink it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from jarvis.api.deps import AppState, Owner, State
from jarvis.db.session import transaction
from jarvis.telegram.api import TelegramError
from jarvis.telegram.bot import TelegramBot

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

NOT_SET_UP = "Telegram isn't set up: add TELEGRAM_BOT_TOKEN to .env (see docs/setup.md)."


def _bot(state: AppState) -> TelegramBot:
    if state.telegram is None:
        raise HTTPException(status_code=503, detail=NOT_SET_UP)
    return state.telegram


@router.get("/status")
async def telegram_status(owner: Owner, state: State) -> dict[str, Any]:
    if state.telegram is None:
        return {"configured": False, "paired": False}
    return await state.telegram.status()


@router.post("/pairing-code")
async def pairing_code(owner: Owner, state: State) -> dict[str, Any]:
    """A one-time link for your Telegram account. Needs a fresh passkey tap."""
    bot = _bot(state)
    if bot.username is None:
        detail = bot.last_error or "The Telegram bot hasn't connected yet. Try again in a moment."
        raise HTTPException(status_code=503, detail=detail)
    async with transaction(state.services.session_factory) as session:
        if not await state.auth.consume_step_up(session, owner):
            raise HTTPException(status_code=403, detail="Confirm with your passkey first.")
        code, link = await bot.create_pairing_link(session)
    return {
        "code": code.code,
        "link": link,
        "bot_username": bot.username,
        "expires_at": code.expires_at.isoformat(),
    }


@router.delete("/owner")
async def unlink(owner: Owner, state: State) -> dict[str, bool]:
    bot = _bot(state)
    async with transaction(state.services.session_factory) as session:
        previous = await bot.unpair(session)
    if previous is None:
        raise HTTPException(status_code=404, detail="Telegram isn't linked.")
    await bot.say_goodbye(previous)
    return {"ok": True}


@router.post("/test")
async def test_message(owner: Owner, state: State) -> dict[str, bool]:
    bot = _bot(state)
    try:
        sent = await bot.send_test_message()
    except TelegramError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not sent:
        raise HTTPException(status_code=409, detail="Link your Telegram account first.")
    return {"ok": True}
