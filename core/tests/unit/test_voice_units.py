"""Voice settings, confirmation phrases and the voice WebSocket protocol."""

from __future__ import annotations

import json
import logging
import wave
from collections.abc import Callable
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
from jarvis.voice.bench import TARGET_SECONDS, BenchError, Report, Turn, read_question
from jarvis.voice.config import (
    VoiceConfigError,
    load_voice_config,
    normalize_phrase,
    parse_voice_config,
)
from jarvis.voice.confirm import classify, strip_wrappers
from jarvis.voice.protocol import VoiceSerializer
from jarvis.voice.speech import SpeechClient, SpeechStatus

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


# --- The speech server's status ------------------------------------------------------

Handler = Callable[[httpx.Request], httpx.Response]


def _speech(handler: Handler) -> SpeechClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://speech/v1/")
    return SpeechClient("http://speech/v1", parse_voice_config(_config()).speech, client=http)


def _models(*ids: str) -> Handler:
    return lambda _request: httpx.Response(200, json={"data": [{"id": i} for i in ids]})


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (_models("stt-model", "tts-model"), SpeechStatus(True, True, True, "ready")),
        (_models("stt-model"), SpeechStatus(True, True, False, "not downloaded: tts-model")),
        (
            lambda _request: httpx.Response(401),
            SpeechStatus(True, False, False, "it refused SPEECH_API_KEY", key_refused=True),
        ),
        (
            lambda _request: httpx.Response(500),
            SpeechStatus(False, False, False, "it answered with error 500"),
        ),
    ],
    ids=["ready", "a model missing", "key refused", "server error"],
)
async def test_speech_server_status(handler: Handler, expected: SpeechStatus) -> None:
    speech = _speech(handler)
    try:
        assert await speech.status() == expected
    finally:
        await speech.aclose()


async def test_an_unreachable_speech_server() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    speech = _speech(refuse)
    try:
        assert await speech.status() == SpeechStatus(
            False, False, False, "unreachable (ConnectError)"
        )
    finally:
        await speech.aclose()


# --- The latency benchmark's verdict --------------------------------------------------


def _turn(speaking: float | None, **flags: bool) -> Turn:
    return Turn(heard=0.3, answered=0.6, speaking=speaking, finished=True, **flags)


def test_the_benchmark_passes_on_a_fast_median() -> None:
    report = Report(asked=3, turns=[_turn(0.9), _turn(1.1), _turn(2.0)])
    assert report.median("speaking") == 1.1
    assert report.p90("speaking") == 2.0
    assert report.passed
    assert report.render().splitlines()[-1].startswith("PASS")


@pytest.mark.parametrize(
    "report",
    [
        Report(asked=3, turns=[_turn(1.6), _turn(1.7), _turn(0.5)]),  # too slow
        Report(asked=3, turns=[_turn(0.5), _turn(0.5), _turn(None)]),  # one unanswered
        Report(asked=3, turns=[_turn(0.5), _turn(0.5)]),  # stopped early
        Report(asked=1, turns=[]),
    ],
    ids=["slow", "unanswered", "stopped early", "nothing"],
)
def test_the_benchmark_fails_unless_every_turn_is_answered_in_time(report: Report) -> None:
    assert not report.passed
    assert report.render().splitlines()[-1].startswith("FAIL")


def test_the_benchmark_says_what_went_wrong() -> None:
    cut_off = Turn(cut_off=True)
    long = Turn(heard=0.3, answered=0.5, speaking=0.8, finished=False)
    report = Report(asked=5, turns=[_turn(0.8, filler=True), cut_off, long])
    text = report.render()
    assert text.startswith("3 of 5 turns")
    assert "(1 turn(s): Jarvis took the turn before the question was over)" in text
    assert "(1 turn(s): still answering after 30 s)" in text
    assert "(1 turn(s): a filler played while the answer started)" in text
    assert "no spoken answer" not in text  # the cut-off turn is explained once
    assert f"under {TARGET_SECONDS:g} s" in text


def test_the_question_must_be_16_khz_mono(tmp_path: Path) -> None:
    loud = b"\x00\x10" * 800 + b"\x00\x00" * 800  # speech, then silence
    path = tmp_path / "q.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(loud)
    pcm, speech_end = read_question(path)
    assert len(pcm) == 3200
    assert speech_end == 1600  # where the last word ends, not the file
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44_100)
        w.writeframes(loud)
    with pytest.raises(BenchError, match="16 kHz mono"):
        read_question(path)
    with pytest.raises(BenchError, match="Can't read"):
        read_question(tmp_path / "missing.wav")
