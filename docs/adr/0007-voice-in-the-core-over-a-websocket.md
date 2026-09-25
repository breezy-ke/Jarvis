# 0007: Voice runs in the core, over one WebSocket, with a local speech server

- Status: accepted
- Date: 2026-09-25

## Context

- **Where voice happens:** the owner talks to Jarvis in three places. The app
  (phone or desktop), a "Hey Jarvis" tray app on the Windows PC, and Telegram
  voice notes.
- **Speed:** Phase 2's target is a median under 1.5 s from the end of a
  question to the first sound of the answer.
- **Privacy:** audio is personal data. Hearing and speaking should happen on
  the PC.
- **One Jarvis:** a spoken request needs the same memory, tools, approvals and
  audit log as a typed one. Voice must never become a side door around the
  policy engine.
- **Cost:** free, on a GPU that Ollama also uses.

The original plan sketched a separate voice service using WebRTC
(`SmallWebRTCTransport`) for the app, calling the core over HTTP.

## Decision

- **The voice pipeline runs inside the core** (Pipecat). Silero VAD and Smart
  Turn find the end of a turn. The same chat service as the app answers, in
  voice mode (a fast model route, short spoken answers). There's no second
  service, and no extra network hop between hearing and thinking.
- **One protocol for every client:** a WebSocket at `/api/voice/ws`. 16 kHz
  PCM goes up and 24 kHz PCM comes down, with small JSON events for the
  transcript, state and confirmations (`core/jarvis/voice/protocol.py`).
  - The app signs in with its session cookie, and the origin is checked.
  - A satellite uses a device token from one-time pairing, stored as a hash.
- **Speech is a separate, local container:** speaches (faster-whisper for
  speech-to-text, Kokoro for text-to-speech). It speaks the OpenAI audio API,
  so the engine behind it can be swapped. It has no published port, needs an
  API key, and logs only warnings, because its debug logs would include what
  was said. The image is pinned, with CPU and CUDA builds.
- **Approving by voice** needs an exact phrase and is bound to the action's
  payload hash. It's never possible for high or critical risk.

## Consequences

- **Good:**
  - One code path for the app and the satellite, tested end to end with
    recorded speech through the real turn detection.
  - A WebSocket works anywhere HTTPS does, including over Tailscale on a
    phone, with no STUN, TURN or ICE to get right.
  - The speech engines can be upgraded or swapped without touching the core.
- **Costs:**
  - WebSocket audio is uncompressed and has no jitter buffer, unlike WebRTC:
    about 32 kB/s up and 48 kB/s down. That's fine on Wi-Fi, 4G and
    Tailscale, but choppier on a poor connection. Echo cancellation is left
    to the browser, and the satellite is half-duplex instead.
  - A crash in the voice pipeline happens in the core's process. Each session
    runs in its own task, and at most two sessions run at once.
  - The pipeline needs NLTK's sentence data. It's baked into the image, and
    fetched once at startup if missing.
  - The GPU build of the speech server needs an NVIDIA driver from 560 or
    later (`make doctor` checks it). The runbook has a fallback for older
    drivers.
- This supersedes the separate `voice/` service and WebRTC transport in the
  original plan.
