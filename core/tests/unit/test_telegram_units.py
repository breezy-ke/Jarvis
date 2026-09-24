"""Telegram pieces that need no database: formatting, parsing, redaction, the HTTP client."""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
import respx

from jarvis.db.models import ActionProposal
from jarvis.policy.state import is_stand_down
from jarvis.security.scanners import redact_text
from jarvis.telegram.api import TelegramAPI, TelegramError
from jarvis.telegram.bot import callback_data, parse_callback, parse_command
from jarvis.telegram.format import balanced, split_markdown, to_html, to_plain, to_speech

TOKEN = "123456789:" + "AAbbCCddEEffGGhhIIjjKKllMMnnOOppQQr"  # shaped like a bot token
ROOT = f"https://api.telegram.org/bot{TOKEN}"


# --- Formatting ----------------------------------------------------------------------------


def test_markdown_becomes_telegram_html() -> None:
    markdown = (
        "## Today\n"
        "- **Kickoff** with *Acme* at 10:00\n"
        "- Review `pnpm build` output\n"
        "See [the brief](https://example.com/brief?a=1&b=2)."
    )
    assert to_html(markdown) == (
        "<b>Today</b>\n"
        "• <b>Kickoff</b> with <i>Acme</i> at 10:00\n"
        "• Review <code>pnpm build</code> output\n"
        'See <a href="https://example.com/brief?a=1&amp;b=2">the brief</a>.'
    )


def test_markup_in_a_reply_is_shown_not_obeyed() -> None:
    result = to_html('Try <script>alert("x")</script> & <b>not bold</b>')
    assert "<script>" not in result
    assert "&lt;script&gt;" in result
    assert "&lt;b&gt;not bold&lt;/b&gt;" in result
    assert to_html("[click](javascript:alert(1))") == "[click](javascript:alert(1))"


def test_code_blocks_keep_their_text() -> None:
    markdown = "```python\nif a < b and snake_case_name:\n    print('**no**')\n```"
    assert to_html(markdown) == (
        '<pre><code class="language-python">if a &lt; b and snake_case_name:\n'
        "    print('**no**')</code></pre>"
    )


def test_underscores_in_names_are_not_italics() -> None:
    assert to_html("set MAX_RETRIES and my_var") == "set MAX_RETRIES and my_var"
    assert to_html("an _emphasised_ word") == "an <i>emphasised</i> word"


def test_broken_nesting_falls_back_to_plain_escaped_text() -> None:
    assert balanced("<b>a <i>b</i></b>")
    assert not balanced("<b>a <i>b</b></i>")
    assert not balanced("<b>open")
    result = to_html("**a *b** c* <x>")
    assert balanced(result)


def test_long_replies_are_split_at_line_breaks() -> None:
    lines = [f"Line {i}: " + "word " * 30 for i in range(60)]
    chunks = split_markdown("\n".join(lines), limit=1_000)
    assert len(chunks) > 1
    assert all(len(c) <= 1_000 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines


def test_a_split_code_block_is_closed_and_reopened() -> None:
    code = "\n".join(f"print({i})" for i in range(200))
    chunks = split_markdown(f"Here:\n```python\n{code}\n```\nDone.", limit=500)
    assert len(chunks) > 2
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk  # every piece renders on its own
        assert balanced(to_html(chunk))


def test_plain_text_and_speech() -> None:
    markdown = "## Plan\n- **Call** [Acme](https://acme.co.ke) about `v2`\n"
    assert to_plain(markdown) == "Plan\n• Call Acme (https://acme.co.ke) about v2"
    assert to_speech(markdown) == "Plan\nCall Acme about v2"


# --- Commands and buttons ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start ABCD2345", ("start", "ABCD2345")),
        ("/START@JarvisTestBot  abcd-2345 ", ("start", "abcd-2345")),
        ("/status", ("status", "")),
        ("/status@SomeOtherBot", (None, "")),  # meant for another bot
        ("hello /status", (None, "")),
        ("/", (None, "")),
    ],
)
def test_parse_command(text: str, expected: tuple[str | None, str]) -> None:
    assert parse_command(text, "JarvisTestBot") == expected


def _proposal() -> ActionProposal:
    fake = SimpleNamespace(id=uuid.uuid4(), payload_hash="ab" * 32)
    return cast(ActionProposal, fake)


def test_buttons_fit_telegram_and_carry_the_payload_hash() -> None:
    proposal = _proposal()
    approve = callback_data("ap", proposal)
    assert len(approve.encode()) <= 64
    assert parse_callback(approve) == ("ap", proposal.id, "ab" * 8)
    assert parse_callback(callback_data("rj", proposal)) == ("rj", proposal.id, "")
    assert parse_callback(callback_data("un", proposal)) == ("un", proposal.id, "")


@pytest.mark.parametrize(
    "data",
    ["", "ap", f"ap:{uuid.uuid4().hex}", "zz:" + uuid.uuid4().hex, "rj:not-a-uuid", "ap:x:y:z"],
)
def test_bad_button_data_is_refused(data: str) -> None:
    assert parse_callback(data) is None


@pytest.mark.parametrize(
    ("text", "meaning"),
    [
        ("Stand down.", True),
        ("Jarvis, stand down!", True),
        ("OK Jarvis, please stand down now", True),
        ("stand down the meeting", False),
        ("withstand down", False),
        ("", False),
    ],
)
def test_stand_down_is_a_whole_phrase(text: str, meaning: bool) -> None:
    assert is_stand_down(text) is meaning


# --- Masking secrets on the way out ---------------------------------------------------------


def test_secrets_and_card_numbers_are_masked() -> None:
    text = (
        "Key AKIAABCDEFGHIJKLMNOP, card 4111 1111 1111 1111, "
        "invoice 1234 5678 9012 3456, KRA PIN A123456789Z."
    )
    clean, hidden = redact_text(text, placeholder="[hidden]")
    assert hidden == 3
    assert "AKIA" not in clean
    assert "4111" not in clean
    assert "A123456789Z" not in clean
    assert "1234 5678 9012 3456" in clean  # fails the card checksum: not a card


# --- The HTTP client -------------------------------------------------------------------------


@respx.mock
async def test_send_message_request() -> None:
    route = respx.post(f"{ROOT}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 7}}
    )
    api = TelegramAPI(TOKEN)
    try:
        sent = await api.send_message(
            42,
            "<b>Hi</b>",
            html=True,
            buttons=[[{"text": "Yes", "callback_data": "ap:x"}]],
            silent=True,
            reply_to=3,
        )
    finally:
        await api.aclose()
    assert sent == {"message_id": 7}
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "chat_id": 42,
        "text": "<b>Hi</b>",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        "disable_notification": True,
        "reply_markup": {"inline_keyboard": [[{"text": "Yes", "callback_data": "ap:x"}]]},
        "reply_parameters": {"message_id": 3, "allow_sending_without_reply": True},
    }


@respx.mock
async def test_telegram_errors_carry_status_and_retry_after() -> None:
    respx.post(f"{ROOT}/getUpdates").respond(
        429,
        json={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 5",
            "parameters": {"retry_after": 5},
        },
    )
    api = TelegramAPI(TOKEN)
    with pytest.raises(TelegramError) as caught:
        await api.get_updates(None, wait=0)
    await api.aclose()
    assert caught.value.status == 429
    assert caught.value.retry_after == 5


@respx.mock
async def test_the_token_never_leaks_into_errors() -> None:
    respx.post(f"{ROOT}/getMe").mock(side_effect=httpx.ConnectError(f"failed: {ROOT}/getMe"))
    respx.post(f"{ROOT}/getFile").respond(
        400, json={"ok": False, "error_code": 400, "description": f"bad url {ROOT}/x"}
    )
    api = TelegramAPI(TOKEN)
    with pytest.raises(TelegramError) as network:
        await api.get_me()
    with pytest.raises(TelegramError) as refused:
        await api.get_file("f")
    await api.aclose()
    for error in (network.value, refused.value):
        assert TOKEN not in str(error)
        assert error.__suppress_context__ or error.__context__ is None
    assert "ConnectError" in str(network.value)


@respx.mock
async def test_the_token_never_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    respx.post(f"{ROOT}/sendChatAction").respond(200, json={"ok": True, "result": True})
    respx.post(f"{ROOT}/getUpdates").respond(200, json={"ok": True, "result": []})
    api = TelegramAPI(TOKEN)
    with caplog.at_level(logging.INFO, logger="httpx"):
        await api.send_chat_action(1, "typing")
        await api.get_updates(None, wait=0)
    await api.aclose()
    assert "sendChatAction" in caplog.text
    assert TOKEN not in caplog.text
    assert "getUpdates" not in caplog.text  # routine long polls are left out


@respx.mock
async def test_voice_is_uploaded_as_multipart() -> None:
    route = respx.post(f"{ROOT}/sendVoice").respond(
        200, json={"ok": True, "result": {"message_id": 9}}
    )
    api = TelegramAPI(TOKEN)
    await api.send_voice(42, b"ID3-audio")
    await api.aclose()
    request = route.calls.last.request
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="chat_id"\r\n\r\n42' in body
    assert b'filename="jarvis.mp3"' in body
    assert b"ID3-audio" in body


@respx.mock
async def test_downloads_are_checked() -> None:
    respx.get(f"https://api.telegram.org/file/bot{TOKEN}/voice/a.oga").respond(
        200, content=b"x" * 2_000
    )
    api = TelegramAPI(TOKEN)
    assert len(await api.download("voice/a.oga")) == 2_000
    with pytest.raises(TelegramError, match="too big"):
        await api.download("voice/a.oga", max_bytes=1_000)
    with pytest.raises(TelegramError, match="unexpected file path"):
        await api.download("../../etc/passwd")
    await api.aclose()
