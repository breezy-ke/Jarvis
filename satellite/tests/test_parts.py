"""Settings, pairing, audio conversion and the wake-word models."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest

from jarvis_satellite import wake
from jarvis_satellite.audio import Framer, Resampler, Speaker, rms
from jarvis_satellite.config import Settings, load, save
from jarvis_satellite.pairing import MemoryStore, PairingError, pair
from jarvis_satellite.protocol import explain_close, parse_event
from tests.conftest import TOKEN

FIXTURES = Path(__file__).parent / "fixtures"


# --- Settings ----------------------------------------------------------------------------


def test_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "satellite.toml"
    original = Settings(server="https://jarvis.tail.ts.net/", name='Desk "PC"', chime=False)
    save(original, path)
    loaded = load(path)
    assert loaded == original
    assert loaded.server == "https://jarvis.tail.ts.net"
    assert loaded.socket_url == "wss://jarvis.tail.ts.net/api/voice/ws"
    assert Settings().socket_url == "ws://localhost:8080/api/voice/ws"


def test_settings_are_kept_sensible(tmp_path: Path) -> None:
    path = tmp_path / "satellite.toml"
    path.write_text("wake_threshold = 5\nfollow_up_seconds = -1\nsurprise = 1\n")
    settings = load(path)
    assert settings.wake_threshold == 0.95
    assert settings.follow_up_seconds == 0.0
    with pytest.raises(ValueError, match="server"):
        Settings(server="localhost:8080")


# --- Pairing -----------------------------------------------------------------------------


def _client(status: int, body: object) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/voice/devices/pair"
        assert json.loads(request.content) == {"code": "ABCD-2345", "name": "Desk PC"}
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pairing_stores_the_token_and_nothing_else(capsys: pytest.CaptureFixture[str]) -> None:
    store = MemoryStore()
    settings = Settings(name="Desk PC")
    pair(settings, " ABCD-2345 ", store=store, client=_client(200, {"token": TOKEN}))
    assert store.get(settings.server) == TOKEN
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (403, {"detail": "no"}, "wrong or has expired"),
        (429, {"detail": "slow down"}, "Too many"),
        (500, {"detail": "boom"}, "HTTP 500"),
        (200, {"token": "not-a-device-token"}, "unexpected reply"),
        (200, ["no", "token"], "unexpected reply"),
    ],
)
def test_pairing_problems_are_explained(status: int, body: object, message: str) -> None:
    store = MemoryStore()
    with pytest.raises(PairingError, match=message):
        pair(Settings(name="Desk PC"), "ABCD-2345", store=store, client=_client(status, body))
    assert store.tokens == {}


def test_pairing_when_jarvis_is_down() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.Client(transport=httpx.MockTransport(down))
    with pytest.raises(PairingError, match="Can't reach Jarvis"):
        pair(Settings(name="Desk PC"), "ABCD-2345", store=MemoryStore(), client=client)


# --- Protocol ----------------------------------------------------------------------------


def test_events_and_close_codes() -> None:
    assert parse_event('{"type": "ready"}') == {"type": "ready"}
    assert parse_event("nope") is None
    assert parse_event('{"no": "type"}') is None
    assert explain_close(1000, "") is None
    assert "Pair it again" in (explain_close(4401, "") or "")
    assert explain_close(4503, "Voice is off.") == "Voice is off."


# --- Audio -------------------------------------------------------------------------------


def _tone(hz: float, rate: int, seconds: float) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def test_resampling_is_the_same_however_it_is_chunked() -> None:
    signal = _tone(440, 48_000, 0.5)
    whole = Resampler(48_000, 16_000).process(signal)
    chunked_resampler = Resampler(48_000, 16_000)
    parts, offset = [], 0
    for size in [128, 7, 333, 1, 1024] * 100:
        if offset >= len(signal):
            break
        parts.append(chunked_resampler.process(signal[offset : offset + size]))
        offset += size
    chunked = np.concatenate(parts)
    assert len(chunked) == len(whole)
    np.testing.assert_allclose(chunked, whole, atol=1e-5)


def test_resampling_keeps_speech_and_blocks_aliasing() -> None:
    def level_after(hz: float) -> float:
        return rms(Resampler(48_000, 16_000).process(_tone(hz, 48_000, 1.0))[200:])

    assert level_after(1_000) > 0.3
    assert level_after(12_000) < 0.02
    # Streaming holds back one input sample (two outputs at 2x) until the next chunk.
    assert 4_798 <= len(Resampler(24_000, 48_000).process(np.zeros(2_400))) <= 4_800


def test_framer_makes_80_ms_frames() -> None:
    framer = Framer()
    assert framer.push(np.zeros(1_000)) == []
    frames = framer.push(np.zeros(2_000))
    assert [len(f) for f in frames] == [1_280, 1_280]


def test_speaker_plays_flushes_and_chimes() -> None:
    speaker = Speaker(rate=48_000)
    speaker.play(b"\x00\x10" * 2_400 + b"\x01")  # 0.1 s at 24 kHz, plus an odd byte
    assert speaker.playing
    out = np.zeros((960, 1), dtype=np.float32)  # 20 ms at 48 kHz
    speaker._fill(out, 960, None, None)
    assert np.abs(out).max() > 0
    speaker.flush()
    assert not speaker.playing
    speaker._fill(out, 960, None, None)
    assert np.abs(out).max() == 0  # nothing left after a flush
    speaker.chime()
    assert speaker.playing


# --- Wake-word models ---------------------------------------------------------------------


def test_models_are_checked_before_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    content = {"a.onnx": b"model a", "b.onnx": b"model b"}
    monkeypatch.setattr(
        wake, "MODELS", {n: hashlib.sha256(c).hexdigest() for n, c in content.items()}
    )
    served = dict(content)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=served[request.url.path.rsplit("/", 1)[-1]])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert sorted(wake.fetch_models(tmp_path, client=client)) == ["a.onnx", "b.onnx"]
    assert wake.models_ready(tmp_path)
    assert wake.fetch_models(tmp_path, client=client) == []  # nothing to do
    (tmp_path / "b.onnx").write_bytes(b"tampered")
    served["b.onnx"] = b"a different file"  # the download doesn't match either
    with pytest.raises(wake.WakeError, match="checksum"):
        wake.fetch_models(tmp_path, client=client)
    assert not wake.models_ready(tmp_path)


def test_without_models_the_wake_word_explains_itself(tmp_path: Path) -> None:
    with pytest.raises(wake.WakeError, match="fetch-models"):
        wake.OpenWakeWord(tmp_path)


def _frames(path: Path) -> list[np.ndarray]:
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels()) == (16_000, 1)
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    padded = np.concatenate([np.zeros(16_000, np.int16), audio, np.zeros(16_000, np.int16)])
    return [padded[i : i + 1_280] for i in range(0, len(padded) - 1_279, 1_280)]


@pytest.mark.skipif(
    importlib.util.find_spec("openwakeword") is None,
    reason="openWakeWord installs on Windows only",
)
def test_the_real_wake_word_model(tmp_path: Path) -> None:
    wake.fetch_models(tmp_path)
    detector = wake.OpenWakeWord(tmp_path, threshold=0.5)
    assert any(detector.detect(f) for f in _frames(FIXTURES / "hey_jarvis.wav"))
    detector.reset()
    assert not any(detector.detect(f) for f in _frames(FIXTURES / "question.wav"))
