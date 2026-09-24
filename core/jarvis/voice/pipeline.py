"""One live voice conversation: a Pipecat pipeline over the voice WebSocket.

    microphone ─► VAD + Smart Turn ─► speech-to-text ─► Jarvis ─► text-to-speech ─► speaker
                   (in-process)        (speech server)   (brain)   (speech server)

Silero VAD and Smart Turn v3 run in this process on the CPU (their models ship
with Pipecat). Speech recognition and synthesis run on the local speech server.
Talking over Jarvis interrupts it (barge-in): the reply stops, the client
drops queued audio, and what was already said is kept in the transcript.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable

from fastapi import WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputTransportMessageFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.utils.time import time_now_iso8601
from pipecat.workers.runner import WorkerRunner

from jarvis.chat.service import ChatService
from jarvis.services import Services
from jarvis.voice.brain import JarvisVoiceBrain, VoiceSessionState, event
from jarvis.voice.config import VoiceConfig
from jarvis.voice.protocol import INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE, VoiceSerializer
from jarvis.voice.speech import TTS_SAMPLE_RATE, SpeechClient, SpeechError

log = logging.getLogger("jarvis.voice")

IDLE_TIMEOUT_SECS = 600  # close a silent session after 10 minutes


class LocalSpeechToText(SegmentedSTTService):
    """Transcribes each complete utterance on the local speech server."""

    def __init__(self, client: SpeechClient) -> None:
        super().__init__(
            sample_rate=INPUT_SAMPLE_RATE,
            # Worst case from end of speech to transcript on the local server
            # (GPU: well under a second). Turn detection waits this long for it.
            ttfs_p99_latency=1.5,
            settings=STTSettings(model=client.config.stt_model, language=None),
        )
        self._client = client

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        await self.start_processing_metrics()
        try:
            text = await self._client.transcribe(audio, filename="speech.wav")
        except SpeechError as exc:
            log.warning("speech-to-text failed: %s", exc)
            await self.push_frame(event("error", message=str(exc)))
            return
        finally:
            await self.stop_processing_metrics()
        if text:
            yield TranscriptionFrame(text, self._user_id, time_now_iso8601())


class LocalTextToSpeech(TTSService):
    """Speaks with Kokoro on the local speech server, streaming PCM as it's made."""

    def __init__(self, client: SpeechClient) -> None:
        super().__init__(
            sample_rate=TTS_SAMPLE_RATE,
            push_start_frame=True,
            push_stop_frames=True,
            settings=TTSSettings(
                model=client.config.tts_model, voice=client.config.voice, language=None
            ),
        )
        self._client = client

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        try:
            await self.start_tts_usage_metrics(text)
            async for chunk in self._client.stream_pcm(text, chunk_size=self.chunk_size):
                await self.stop_ttfb_metrics()
                yield TTSAudioRawFrame(chunk, self.sample_rate, 1, context_id=context_id)
        except SpeechError as exc:
            log.warning("text-to-speech failed: %s", exc)
            await self.push_frame(event("error", message=str(exc)))


class ClientControl(FrameProcessor):
    """Handles control messages from the client, e.g. a tap to stop Jarvis speaking."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputTransportMessageFrame):
            if direction == FrameDirection.DOWNSTREAM and frame.message.get("type") == "interrupt":
                await self.broadcast_interruption()
            return  # control messages stop here
        await self.push_frame(frame, direction)


class ClientEvents(FrameProcessor):
    """Tells the client whether Jarvis is listening or speaking."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        state = None
        if isinstance(frame, UserStartedSpeakingFrame):
            state = "listening"
        elif isinstance(frame, BotStartedSpeakingFrame):
            state = "speaking"
        elif isinstance(frame, BotStoppedSpeakingFrame):
            state = "idle"
        await self.push_frame(frame, direction)
        if state is not None:
            # Bot-speaking frames travel upstream from the output transport;
            # the event goes back down to it either way.
            await self.push_frame(event("state", state=state))


def build_pipeline(
    transport: FastAPIWebsocketTransport,
    brain: JarvisVoiceBrain,
    speech: SpeechClient,
) -> Pipeline:
    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )
    return Pipeline(
        [
            transport.input(),
            ClientControl(),
            LocalSpeechToText(speech),
            user_aggregator,
            brain,
            LocalTextToSpeech(speech),
            ClientEvents(),
            transport.output(),
            assistant_aggregator,
        ]
    )


async def run_voice_session(
    websocket: WebSocket,
    services: Services,
    chat: ChatService,
    config: VoiceConfig,
    speech: SpeechClient,
    *,
    channel: str,
    state: VoiceSessionState | None = None,
    on_end: Callable[[], None] | None = None,
) -> None:
    """Run one voice conversation until the client disconnects (the socket is accepted)."""
    transport = FastAPIWebsocketTransport(
        websocket,
        FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_in_sample_rate=INPUT_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
            audio_out_auto_silence=False,
            audio_out_end_silence_secs=0,
            add_wav_header=False,
            serializer=VoiceSerializer(),
            allowed_origins=[],  # the API checked the origin before accepting
        ),
    )
    brain = JarvisVoiceBrain(
        services=services, chat=chat, config=config, channel=channel, state=state
    )
    worker = PipelineWorker(
        build_pipeline(transport, brain, speech),
        params=PipelineParams(
            audio_in_sample_rate=INPUT_SAMPLE_RATE,
            audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
            enable_metrics=True,
        ),
        enable_rtvi=False,
        idle_timeout_secs=IDLE_TIMEOUT_SECS,
    )

    @transport.event_handler("on_client_connected")
    async def _connected(_transport: object, _client: object) -> None:
        await worker.queue_frames(
            [
                event("ready", input_rate=INPUT_SAMPLE_RATE, output_rate=OUTPUT_SAMPLE_RATE),
                event("state", state="idle"),
            ]
        )

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_transport: object, _client: object) -> None:
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    try:
        await runner.run()
    finally:
        if on_end is not None:
            on_end()
