"""Voice settings, confirmation phrases and the voice WebSocket protocol."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageUrgentFrame,
    TextFrame,
)

from jarvis.api.voice import _short
from jarvis.config import REPO_ROOT
from jarvis.voice import textdata
from jarvis.voice.config import (
    VoiceConfigError,
    load_voice_config,
    normalize_phrase,
    parse_voice_config,
)
from jarvis.voice.confirm import classify, strip_wrappers
from jarvis.voice.protocol import VoiceSerializer

REPO_VOICE = REPO_ROOT / "config" / "voice.yaml"


SPEECH = {"stt_model": "stt-model", "tts_model": "tts-model", "voice": "bm_george"}


def _config(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "version": 1,
        "speech": SPEECH,
        "confirm_phrases": ["confirm", "go ahead"],
        "cancel_phrases": ["cancel"],
    }
    raw.update(overrides)
    return raw


def test_the_shipped_voice_settings_are_valid() -> None:
    config = load_voice_config(REPO_VOICE)
    assert config.speech.voice.startswith("bm_")  # a British male voice
    assert "confirm" in config.confirm_phrases
    assert config.speech.fixed_language is None  # auto-detect English or Swahili


def test_phrases_are_normalized() -> None:
    config = parse_voice_config(_config(confirm_phrases=["  Go Ahead! ", "CONFIRM."]))
    assert config.confirm_phrases == ("go ahead", "confirm")
    assert normalize_phrase("Don’t!") == "don't"


@pytest.mark.parametrize("casual", ["yes", "OK", "Sure.", "yeah"])
def test_a_casual_word_can_never_approve(casual: str) -> None:
    with pytest.raises(VoiceConfigError, match="casual"):
        parse_voice_config(_config(confirm_phrases=[casual]))


def test_a_phrase_cannot_both_confirm_and_cancel() -> None:
    with pytest.raises(VoiceConfigError, match="both"):
        parse_voice_config(_config(confirm_phrases=["stop now"], cancel_phrases=["stop now"]))


def test_language_must_be_auto_or_a_code() -> None:
    config = parse_voice_config(_config(speech={**SPEECH, "language": "SW"}))
    assert config.speech.fixed_language == "sw"
    with pytest.raises(VoiceConfigError):
        parse_voice_config(_config(speech={**SPEECH, "language": "?"}))


@pytest.mark.parametrize(
    ("heard", "meaning"),
    [
        ("Confirm.", "confirm"),
        ("Jarvis, confirm please", "confirm"),
        ("OK, go ahead.", "confirm"),
        ("Cancel!", "cancel"),
        ("no thanks", None),
        ("yes", None),
        ("confirm the meeting for tomorrow", None),  # a new request, not an answer
        # Jarvis's own read-back, heard by a microphone, must never approve:
        ("Say confirm to go ahead, or cancel.", None),
        ("", None),
    ],
)
def test_only_an_exact_phrase_counts(heard: str, meaning: str | None) -> None:
    config = parse_voice_config(_config())
    assert classify(heard, config) == meaning


def test_strip_wrappers() -> None:
    assert strip_wrappers("Jarvis, please confirm, thank you") == "confirm"
    assert strip_wrappers("please") == ""


async def test_serializer_sends_audio_as_binary_and_events_as_json() -> None:
    serializer = VoiceSerializer()
    audio = OutputAudioRawFrame(audio=b"\x01\x02" * 4, sample_rate=24_000, num_channels=1)
    assert await serializer.serialize(audio) == b"\x01\x02" * 4
    assert json.loads(str(await serializer.serialize(InterruptionFrame()))) == {"type": "interrupt"}
    urgent = OutputTransportMessageUrgentFrame(message={"type": "user", "text": "habari"})
    assert json.loads(str(await serializer.serialize(urgent))) == {"type": "user", "text": "habari"}
    assert await serializer.serialize(TextFrame("not for the wire")) is None


async def test_serializer_reads_microphone_audio_and_controls() -> None:
    serializer = VoiceSerializer()
    frame = await serializer.deserialize(b"\x00\x01\x02")  # an odd byte is carried over
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.audio == b"\x00\x01"
    assert frame.sample_rate == 16_000
    frame = await serializer.deserialize(b"\x03")
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.audio == b"\x02\x03"
    control = await serializer.deserialize(json.dumps({"type": "interrupt"}))
    assert isinstance(control, InputTransportMessageFrame)
    assert control.message == {"type": "interrupt"}
    assert await serializer.deserialize("not json") is None
    assert await serializer.deserialize(json.dumps(["no", "type"])) is None
    assert await serializer.deserialize(json.dumps({"type": "x", "pad": "a" * 70_000})) is None


def test_pipecat_debug_lines_never_reach_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    from loguru import logger

    with caplog.at_level(logging.DEBUG):
        logger.debug("LocalTextToSpeech: Generating TTS [your PIN is 4821]")
        logger.info("Transcription: meet Achieng at 3")
        logger.warning("pipeline stalled")
    assert "4821" not in caplog.text
    assert "Achieng" not in caplog.text
    assert "pipeline stalled" in caplog.text  # warnings still come through


def test_sentence_data_is_fetched_once_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = {"installed": False, "fetched": 0}

    def fetch(target: Path) -> bool:
        state["fetched"] += 1
        state["installed"] = True
        return True

    monkeypatch.setenv("NLTK_DATA", str(tmp_path))
    monkeypatch.setattr(textdata, "punkt_available", lambda: state["installed"])
    monkeypatch.setattr(textdata, "fetch_punkt", fetch)
    assert textdata.nltk_data_dir() == tmp_path
    assert textdata.ensure_punkt() is None
    assert textdata.ensure_punkt() is None
    assert state["fetched"] == 1


def test_missing_sentence_data_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(target: Path) -> bool:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(textdata, "punkt_available", lambda: False)
    monkeypatch.setattr(textdata, "fetch_punkt", offline)
    problem = textdata.ensure_punkt()
    assert problem is not None
    assert "ConnectError" in problem
    assert "make update" in problem


def test_close_reasons_fit_the_websocket_limit() -> None:
    assert _short("Voice is off.") == "Voice is off."
    long = "ü" * 200  # two bytes each
    short = _short(long)
    assert len(short.encode()) <= 123
    assert short.endswith("…")
