"""The satellite's conversation loop, against a fake Jarvis speaking the real protocol."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from jarvis_satellite.config import Settings
from jarvis_satellite.pairing import MemoryStore
from jarvis_satellite.session import Satellite, _Call
from tests.conftest import (
    SILENCE,
    SPEECH,
    TOKEN,
    WAKE,
    FakeIndicator,
    FakeJarvis,
    FakeMic,
    FakeSpeaker,
    FakeWake,
    eventually,
)


@dataclass
class Rig:
    satellite: Satellite
    mic: FakeMic
    speaker: FakeSpeaker
    wake: FakeWake
    indicator: FakeIndicator
    task: asyncio.Task[None]


@pytest.fixture
async def rig(settings: Settings, tokens: MemoryStore) -> AsyncIterator[Rig]:
    mic, speaker, wake, indicator = FakeMic(), FakeSpeaker(), FakeWake(), FakeIndicator()
    satellite = Satellite(
        settings, tokens=tokens, mic=mic, speaker=speaker, wake=wake, indicator=indicator
    )
    task = asyncio.create_task(satellite.run())
    yield Rig(satellite, mic, speaker, wake, indicator, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_nothing_leaves_the_pc_before_the_wake_word(rig: Rig, jarvis: FakeJarvis) -> None:
    rig.mic.say(SPEECH, 30)
    await eventually(lambda: rig.wake.fed >= 30)
    assert jarvis.connections == 0
    assert rig.indicator.names == ["sleeping"]


async def test_a_conversation(rig: Rig, jarvis: FakeJarvis) -> None:
    rig.mic.say(WAKE)
    await eventually(lambda: jarvis.connections == 1)
    assert jarvis.authorization == [f"Bearer {TOKEN}"]
    assert rig.speaker.chimes == 1  # "I'm listening"
    rig.mic.say(SPEECH, 6)
    await eventually(lambda: rig.speaker.played > 0)  # Jarvis answered out loud
    await eventually(lambda: rig.indicator.names[-1] == "sleeping")  # then went back to sleep
    await eventually(lambda: jarvis.open == 0)
    names = rig.indicator.names
    for state in ("connecting", "listening", "thinking", "speaking"):
        assert state in names
    assert names.index("connecting") < names.index("thinking") < names.index("speaking")
    assert jarvis.speech_heard() == 6
    assert rig.wake.resets == 1


async def test_what_you_say_while_it_connects_is_kept(rig: Rig, jarvis: FakeJarvis) -> None:
    jarvis.ready_delay = 0.3  # "Hey Jarvis, what's on today?" in one breath
    rig.mic.say(WAKE)
    rig.mic.say(SPEECH, 5)
    await eventually(lambda: jarvis.speech_heard() == 5)
    assert all(chunk[:2] == b"\xe8\x03" for chunk in jarvis.audio[:5])  # 1000, in order


async def test_jarvis_never_hears_itself_but_hey_jarvis_interrupts(
    rig: Rig, jarvis: FakeJarvis
) -> None:
    jarvis.speak_seconds = 1.5
    rig.mic.say(WAKE)
    await eventually(lambda: jarvis.connections == 1)
    rig.mic.say(SPEECH, 5)
    await eventually(lambda: rig.speaker.playing)
    heard = len(jarvis.audio)
    rig.mic.say(SPEECH, 5)  # the microphone picks up Jarvis's own voice...
    await eventually(rig.mic.queue.empty)
    await asyncio.sleep(0.1)
    assert rig.speaker.playing
    assert len(jarvis.audio) == heard  # ...and none of it is sent
    rig.mic.say(WAKE)  # "Hey Jarvis!" over Jarvis
    await eventually(lambda: jarvis.controls == [{"type": "interrupt"}])
    assert rig.speaker.flushes >= 1
    rig.mic.say(SPEECH, 2)
    await eventually(lambda: len(jarvis.audio) == heard + 2)  # listening again


async def test_waits_longer_while_an_action_needs_confirming(rig: Rig, jarvis: FakeJarvis) -> None:
    jarvis.confirmation = True
    jarvis.speak_seconds = 0.1
    rig.mic.say(WAKE)
    await eventually(lambda: jarvis.connections == 1)
    rig.mic.say(SPEECH, 5)
    await eventually(lambda: rig.speaker.played > 0)
    await asyncio.sleep(0.9)  # past the 0.4 s follow-up window
    assert jarvis.open == 1  # still listening for "confirm" or "cancel"
    await eventually(lambda: jarvis.open == 0, seconds=3)


async def test_mute_hangs_up_and_ignores_the_microphone(rig: Rig, jarvis: FakeJarvis) -> None:
    rig.mic.say(WAKE)
    await eventually(lambda: jarvis.open == 1)
    rig.satellite.set_muted(True)
    await eventually(lambda: jarvis.open == 0)
    fed = rig.wake.fed
    rig.mic.say(WAKE, 3)
    await eventually(rig.mic.queue.empty)
    await asyncio.sleep(0.1)
    assert rig.wake.fed == fed  # muted: not even the wake word listens
    assert jarvis.connections == 1
    assert rig.indicator.last == ("muted", None)
    rig.satellite.set_muted(False)
    rig.mic.say(WAKE)
    await eventually(lambda: jarvis.connections == 2)


async def test_a_revoked_token_says_pair_again(
    rig: Rig, jarvis: FakeJarvis, tokens: MemoryStore
) -> None:
    tokens.set(jarvis.url, "jv_revoked")
    rig.mic.say(WAKE)
    await eventually(lambda: rig.indicator.last[0] == "unpaired")
    assert "Pair it again" in (rig.indicator.last[1] or "")


async def test_not_paired_yet(rig: Rig, jarvis: FakeJarvis, tokens: MemoryStore) -> None:
    tokens.delete(jarvis.url)
    rig.mic.say(WAKE)
    await eventually(lambda: rig.indicator.last[0] == "unpaired")
    assert jarvis.connections == 0


async def test_voice_unavailable_is_explained(rig: Rig, jarvis: FakeJarvis) -> None:
    jarvis.close_with = (4503, "Voice is missing its sentence data.")
    rig.mic.say(WAKE)
    await eventually(lambda: rig.indicator.last == ("error", "Voice is missing its sentence data."))


async def test_jarvis_unreachable() -> None:
    settings = Settings(server="http://127.0.0.1:9", follow_up_seconds=0.2)
    store = MemoryStore()
    store.set(settings.server, TOKEN)
    mic, indicator = FakeMic(), FakeIndicator()
    satellite = Satellite(
        settings,
        tokens=store,
        mic=mic,
        speaker=FakeSpeaker(),
        wake=FakeWake(),
        indicator=indicator,
    )
    task = asyncio.create_task(satellite.run())
    mic.say(WAKE)
    # Windows takes a couple of seconds to refuse a connection to a closed local port.
    await eventually(lambda: indicator.last[0] == "error", seconds=12)
    assert "Can't reach Jarvis" in (indicator.last[1] or "")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_token_never_reaches_the_logs(
    rig: Rig, jarvis: FakeJarvis, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        rig.mic.say(WAKE)
        rig.mic.say(SPEECH, 6)
        await eventually(lambda: rig.speaker.played > 0)
        await eventually(lambda: rig.indicator.names[-1] == "sleeping")
    ours = [r for r in caplog.records if not r.name.startswith("websockets.server")]
    assert ours  # the satellite logged something... (the fake server's own logs aside)
    assert all(TOKEN not in r.getMessage() for r in ours)  # ...but never the token


def test_half_duplex_rules() -> None:
    speaker = FakeSpeaker()
    satellite = Satellite(
        Settings(),
        tokens=MemoryStore(),
        mic=FakeMic(),
        speaker=speaker,
        wake=FakeWake(),
        indicator=FakeIndicator(),
    )
    call = _Call(satellite, asyncio.Queue())
    assert call.decide(woke=False) == "send"
    call.server_state = "speaking"
    assert call.decide(woke=False) == "drop"
    assert call.decide(woke=True) == "interrupt"
    call.server_state = "idle"
    speaker.play(b"\x00\x00" * 24_000)  # still coming out of the speaker
    assert call.decide(woke=False) == "drop"
    speaker.flush()
    assert call.decide(woke=False) == "send"
    assert SILENCE == 0
