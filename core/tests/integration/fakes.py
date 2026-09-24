"""Stand-ins for outside services (speech server, Telegram) and a few test actions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from jarvis.config import REPO_ROOT
from jarvis.policy.registry import ActionSpec, ExecutionContext
from jarvis.services import Services
from jarvis.telegram.api import TelegramError
from jarvis.voice.config import load_voice_config
from jarvis.voice.speech import SpeechStatus

# --- The local speech server (faster-whisper + Kokoro) --------------------------------------


class FakeSpeech:
    def __init__(self, transcript: str) -> None:
        self.config = load_voice_config(REPO_ROOT / "config" / "voice.yaml").speech
        self.transcript = transcript
        self.heard: list[bytes] = []
        self.said: list[str] = []

    async def transcribe(
        self, audio: bytes, *, filename: str = "", prompt: str | None = None
    ) -> str:
        self.heard.append(audio)
        return self.transcript

    async def stream_pcm(self, text: str, *, chunk_size: int = 4_800) -> AsyncIterator[bytes]:
        self.said.append(text)
        for _ in range(3):
            yield b"\x00\x00" * 2_400  # 0.1 s of silence at 24 kHz

    async def synthesize(self, text: str, *, response_format: str = "mp3") -> bytes:
        self.said.append(text)
        return b"ID3-fake-mp3"

    async def status(self) -> SpeechStatus:
        return SpeechStatus(True, True, True, "ready")

    async def aclose(self) -> None:
        return None


# --- Telegram ----------------------------------------------------------------------------------

AMANI = {"id": 5_550_001, "is_bot": False, "first_name": "Amani", "username": "amani_dev"}
STRANGER = {"id": 5_550_666, "is_bot": False, "first_name": "Eve"}


@dataclass
class Sent:
    chat_id: int
    text: str
    html: bool = False
    buttons: list[list[dict[str, str]]] | None = None
    silent: bool = False
    reply_to: int | None = None
    message_id: int = 0


@dataclass
class FakeTelegram:
    """Stands in for the Telegram Bot API and records everything the bot sends."""

    username: str = "JarvisTestBot"
    inbox: list[dict[str, Any]] = field(default_factory=list)
    sent: list[Sent] = field(default_factory=list)
    edits: list[Sent] = field(default_factory=list)
    answers: list[tuple[str, str | None, bool]] = field(default_factory=list)
    voices: list[tuple[int, bytes]] = field(default_factory=list)
    actions: list[tuple[int, str]] = field(default_factory=list)
    commands: list[tuple[str, str]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    webhook_deleted: bool = False
    refuse_html: bool = False
    refuse_url_buttons: bool = False
    get_me_error: TelegramError | None = None
    _message_id: int = 1_000

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [s.text for s in self.sent if chat_id is None or s.chat_id == chat_id]

    async def get_me(self) -> dict[str, Any]:
        if self.get_me_error is not None:
            raise self.get_me_error
        return {"id": 1, "is_bot": True, "username": self.username}

    async def get_webhook_info(self) -> dict[str, Any]:
        return {"url": "" if self.webhook_deleted else "https://old.example/hook"}

    async def delete_webhook(self) -> None:
        self.webhook_deleted = True

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        self.commands = list(commands)

    async def get_updates(self, offset: int | None, *, wait: int) -> list[dict[str, Any]]:
        pending = [u for u in self.inbox if offset is None or u["update_id"] >= offset]
        if not pending:
            await asyncio.sleep(min(wait, 0.02))  # a long poll with nothing new
        return pending

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
        if html and self.refuse_html:
            raise TelegramError("Bad Request: can't parse entities", status=400)
        has_url = any("url" in b for row in buttons or [] for b in row)
        if has_url and self.refuse_url_buttons:
            raise TelegramError("Bad Request: BUTTON_URL_INVALID", status=400)
        self._message_id += 1
        self.sent.append(
            Sent(chat_id, text, html, buttons, silent, reply_to, message_id=self._message_id)
        )
        return {"message_id": self._message_id}

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        html: bool = False,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.edits.append(Sent(chat_id, text, html, buttons, message_id=message_id))

    async def answer_callback_query(
        self, callback_id: str, text: str | None = None, *, alert: bool = False
    ) -> None:
        self.answers.append((callback_id, text, alert))

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))

    async def send_voice(
        self, chat_id: int, audio: bytes, *, filename: str = "", mime: str = ""
    ) -> dict[str, Any]:
        self.voices.append((chat_id, audio))
        self._message_id += 1
        return {"message_id": self._message_id}

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return {"file_id": file_id, "file_path": f"voice/{file_id}.oga"}

    async def download(self, file_path: str, *, max_bytes: int = 0) -> bytes:
        return self.files[file_path]

    async def aclose(self) -> None:
        return None


class Updates:
    """Builds Telegram updates the way Telegram sends them."""

    def __init__(self) -> None:
        self._next = 1

    def _id(self) -> int:
        self._next += 1
        return self._next

    def message(
        self,
        text: str | None = None,
        *,
        sender: dict[str, Any] = AMANI,
        chat_type: str = "private",
        **extra: Any,
    ) -> dict[str, Any]:
        update_id = self._id()
        chat_id = sender["id"] if chat_type == "private" else -100_123
        message: dict[str, Any] = {
            "message_id": 20_000 + update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": chat_type},
            "from": sender,
            **extra,
        }
        if text is not None:
            message["text"] = text
        return {"update_id": update_id, "message": message}

    def press(
        self, data: str, *, sender: dict[str, Any] = AMANI, message_id: int = 1_001
    ) -> dict[str, Any]:
        update_id = self._id()
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb{update_id}",
                "from": sender,
                "data": data,
                "message": {
                    "message_id": message_id,
                    "chat": {"id": sender["id"], "type": "private"},
                },
            },
        }


# --- Actions for approval tests ------------------------------------------------------------------


class InvitePayload(BaseModel):
    title: str
    attendees: list[str]


class DeployPayload(BaseModel):
    project: str


async def _noop(ctx: ExecutionContext, payload: BaseModel) -> dict[str, Any]:
    return {}


def register_test_actions(services: Services) -> None:
    """calendar.invite (medium risk) and deploy.production (high risk), doing nothing."""
    services.registry.register(
        ActionSpec("calendar.invite", InvitePayload, _noop, lambda p: f"Invite to {p.title}")
    )
    services.registry.register(
        ActionSpec("deploy.production", DeployPayload, _noop, lambda p: f"Deploy {p.project}")
    )
