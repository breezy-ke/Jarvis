"""A small client for the Telegram Bot API.

Only the handful of methods Jarvis uses, over plain HTTPS. Jarvis reads its
messages by long polling, so it needs no public address.

The bot token is part of every request URL, so it's kept out of logs (httpx
logs each URL) and out of error messages.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

API_ROOT = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4_096
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # the Bot API won't hand over bigger files
ALLOWED_UPDATES = ("message", "callback_query")

_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
_KNOWN_TOKENS: set[str] = set()
HIDDEN = "<bot-token>"


def redact_token(text: str) -> str:
    for token in _KNOWN_TOKENS:
        text = text.replace(token, HIDDEN)
    return _TOKEN_RE.sub(HIDDEN, text)


class _HideBotToken(logging.Filter):
    """Mask bot tokens in httpx's request log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "api.telegram.org" not in message:
            return True
        if "/getUpdates" in message and " 200 " in message:
            return False  # a long poll every half minute is noise
        record.msg, record.args = redact_token(message), None
        return True


def hide_token_in_logs(token: str) -> None:
    _KNOWN_TOKENS.add(token)
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, _HideBotToken) for f in logger.filters):
        logger.addFilter(_HideBotToken())


class TelegramError(Exception):
    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(redact_token(message))
        self.status = status
        self.retry_after = retry_after


def _form_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


class TelegramAPI:
    def __init__(
        self, token: str, *, http: httpx.AsyncClient | None = None, root: str = API_ROOT
    ) -> None:
        self._token = token.strip()
        self._root = root.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owns_http = http is None
        hide_token_in_logs(self._token)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        http_timeout: float | None = None,
    ) -> Any:
        url = f"{self._root}/bot{self._token}/{method}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        extra: dict[str, Any] = {} if http_timeout is None else {"timeout": http_timeout}
        try:
            if files:
                form = {k: _form_value(v) for k, v in clean.items()}
                response = await self._http.post(url, data=form, files=files, **extra)
            else:
                response = await self._http.post(url, json=clean, **extra)
        except httpx.HTTPError as exc:
            # `from None`: the original error's message and traceback carry the URL.
            raise TelegramError(f"Can't reach Telegram ({type(exc).__name__}).") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(
                f"Telegram sent an unexpected reply (HTTP {response.status_code}).",
                status=response.status_code,
            ) from None
        if not isinstance(body, dict) or not body.get("ok"):
            body = body if isinstance(body, dict) else {}
            parameters = body.get("parameters") or {}
            raise TelegramError(
                str(body.get("description") or f"Telegram error (HTTP {response.status_code})."),
                status=int(body.get("error_code") or response.status_code),
                retry_after=parameters.get("retry_after"),
            )
        return body.get("result")

    # --- The methods Jarvis uses ------------------------------------------------------

    async def get_me(self) -> dict[str, Any]:
        return dict(await self.call("getMe"))

    async def get_webhook_info(self) -> dict[str, Any]:
        return dict(await self.call("getWebhookInfo"))

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook")

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in commands]},
        )

    async def get_updates(self, offset: int | None, *, wait: int) -> list[dict[str, Any]]:
        """Updates after `offset`, waiting up to `wait` seconds for one to arrive."""
        result = await self.call(
            "getUpdates",
            {"offset": offset, "timeout": wait, "allowed_updates": list(ALLOWED_UPDATES)},
            http_timeout=wait + 15,
        )
        return list(result or [])

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        html: bool = False,
        buttons: list[list[dict[str, str]]] | None = None,
        silent: bool = False,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML" if html else None,
            # Never fetch previews: they'd load remote pages the owner didn't open.
            "link_preview_options": {"is_disabled": True},
            "disable_notification": True if silent else None,
            "reply_markup": {"inline_keyboard": buttons} if buttons else None,
        }
        if reply_to is not None:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        return dict(await self.call("sendMessage", params))

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        html: bool = False,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML" if html else None,
                "link_preview_options": {"is_disabled": True},
                # Leaving the keyboard out removes it.
                "reply_markup": {"inline_keyboard": buttons} if buttons else None,
            },
        )

    async def answer_callback_query(
        self, callback_id: str, text: str | None = None, *, alert: bool = False
    ) -> None:
        await self.call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text[:200] if text else None,
                "show_alert": True if alert else None,
            },
        )

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        await self.call("sendChatAction", {"chat_id": chat_id, "action": action})

    async def send_voice(
        self,
        chat_id: int,
        audio: bytes,
        *,
        filename: str = "jarvis.mp3",
        mime: str = "audio/mpeg",
    ) -> dict[str, Any]:
        return dict(
            await self.call(
                "sendVoice",
                {"chat_id": chat_id},
                files={"voice": (filename, audio, mime)},
                http_timeout=60,
            )
        )

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return dict(await self.call("getFile", {"file_id": file_id}))

    async def download(self, file_path: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
        if file_path.startswith("/") or ".." in file_path.split("/"):
            raise TelegramError("Telegram gave an unexpected file path.")
        url = f"{self._root}/file/bot{self._token}/{file_path}"
        chunks: list[bytes] = []
        size = 0
        try:
            async with self._http.stream("GET", url, timeout=60) as response:
                if response.status_code != 200:
                    raise TelegramError(
                        f"Couldn't download the file (HTTP {response.status_code}).",
                        status=response.status_code,
                    )
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise TelegramError("That file is too big.")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise TelegramError(f"Can't reach Telegram ({type(exc).__name__}).") from None
        return b"".join(chunks)
