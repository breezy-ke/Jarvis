# Architecture

Jarvis is a single-user assistant that runs on your own PC. It has these
parts:

- **core**: a Python API that holds the agents, memory, policy engine, model
  router, voice pipeline and Telegram bot.
- **web**: a React app (PWA) served by the core.
- **speech**: a local speech server that hears (faster-whisper) and speaks
  (Kokoro), used only by the core.
- **satellite** (optional): a Windows tray app that listens for "Hey Jarvis".

Everything stateful lives in one Postgres database.

```
  Phone / laptop browser (PWA)                       Windows PC
          │  HTTPS and WebSocket over Tailscale       ┌─────────────────────────────────────────────────────────┐
          └──────────────────────────────────────────►│ tailscale serve :443 ─► 127.0.0.1:8080                  │
                                                      │ "Hey Jarvis" tray app (satellite) ─► 127.0.0.1:8080     │
                                                      │                                                         │
                                                      │ WSL2 Ubuntu · Docker Engine (+GPU)                      │
                                                      │ ┌─────────────────────────────────────────────────────┐ │
                                                      │ │ core (FastAPI, :8080)                               │ │
                                                      │ │   API, PWA, voice, Telegram and background work     │ │
                                                      │ └──┬────────────┬────────────┬────────────┬───────────┘ │
                                                      │    │            │            │            │             │
                                                      │ ┌──▼────────┐ ┌─▼────────┐ ┌─▼────────┐ ┌─▼────────┐    │
                                                      │ │postgres 17│ │ ollama   │ │ speech   │ │ phoenix  │    │
                                                      │ │+ pgvector │ │ (GPU)    │ │ (GPU)    │ │ traces   │    │
                                                      │ └───────────┘ └──────────┘ └──────────┘ └──────────┘    │
                                                      └─────────────────────────────────────────────────────────┘
   Cloud AI (only the data each is allowed): Groq · Gemini · OpenRouter · (paid, off by default)
   Google (once you connect it): the core checks Gmail every minute and calls Calendar
   Telegram (if you set it up): the core fetches your messages; nothing listens publicly
```

- **Ports:** every port binds to `127.0.0.1`. Postgres and the speech server
  have no published port. Postgres sits on an `internal` Docker network with
  no internet access.
- **Startup chain:** the Windows boot task keeps WSL alive. systemd starts
  Docker, then the `jarvis` service runs `make boot-up`.
- **Crash recovery:** containers restart on failure, and durable workflows
  resume after a crash.

## Request flows

**Chat** (`POST /api/chat`, Server-Sent Events):

1. The chat service loads the conversation and builds context. That context is
   the compact profile summary, plus memory retrieved for the message by
   hybrid search.
2. The orchestrator agent (Pydantic AI) runs with a model chosen by the router
   for the `chat` task, which is personal data, so it runs on a local or
   private-safe model. Tokens stream back as SSE.
3. The agent's tools are deliberately narrow:
   - `search_memory`
   - `remember`
   - `forget`
   - `get_status`
   - `list_capabilities`
   - `inbox_overview` and `draft_email_reply` (email; a draft always waits
     for approval)
   - `propose_action`, the only way to affect anything outside Jarvis

   Once someone else's words (an email, a forwarded message) are in what the
   agent sees, from a tool or from the recent conversation, `remember` and
   `forget` refuse, and every proposal waits for you.
4. Afterwards, a background job extracts new facts from the conversation. They
   are stored as `inferred`, so they wait for your review.

**Actions** (see [security.md](security.md) for the guarantees):

```
agent ─► propose_action ─► validators ─► policy decision ─┬─► refused / draft_only
                                                          ├─► pending ─► you approve (hash-pinned) ─┐
                                                          └─► auto-approved (L3, gate open)  ───────┤
                                                                                                    ▼
                               executor loop (every 2 s, skips everything while the kill switch is on)
                                     ─► executing (committed) ─► executed | failed | unknown_outcome
```

**Email** (`core/jarvis/mail/`; [ADR 0009](adr/0009-gmail-by-rest-polling-and-recipients-by-code.md)):

1. **Sync:** the first time, Jarvis reads the last 14 days of your inbox and
   sent mail. Then every minute it asks Gmail what changed since last time
   (`history.list`). If Gmail has forgotten that point (after about a week
   offline), it reads the recent mail again. Everything that carries content
   is encrypted before it's stored.
2. **Sort:** code checks each new email first: authentication results,
   look-alike senders, a Reply-To elsewhere, risky links, hidden text and
   AI-directed phrasing. Then a model with no tools (the `triage` task) sorts
   it and writes a summary, tasks and dates. Rules the model can't override
   come last: hard warning signs make it suspicious, VIPs are important, and
   newsletters from strangers skip the model altogether.
3. **After sorting:**
   - a `Jarvis/…` label (`email.label`, L3, once autonomy is on)
   - an instant alert for urgent email from someone you know
   - an automatic draft when it needs a reply from someone you know
4. **Draft:** a model with no tools (the `drafting` task) writes only the
   body. Code works out the recipients, the subject and the threading headers
   from the conversation, then proposes `email.send` (always L2) and a Gmail
   copy (`email.draft`, L3).
5. **Send:** after your approval and the 60-second undo, the exact approved
   email goes out, marked with an `X-Jarvis-Action` header. If Gmail's answer
   is lost, the send is "unknown outcome" until that marker turns up in your
   Sent mail. It's never sent twice.
6. **Digests** at 07:15 and 17:30 are built by code, not a model.

```
Gmail ─► sync (every minute) ─► encrypted store ─► signals (code) ─► triage model (no tools) ─► rules
                                                                                                  │
    Inbox · Jarvis/… labels · alerts (people you know) · digests ◄────────────────────────────────┤
                                                                                                  ▼
            you approve ─► 60 s undo ─► Gmail send ◄─ email.send (L2) ◄─ recipients by code ◄─ drafting model (body only)
```

**Voice** (`/api/voice/ws`, a WebSocket; `core/jarvis/voice/protocol.py`
describes it):

1. The Talk page, or the satellite, streams your microphone as 16 kHz PCM.
   The app signs in with your session, and a satellite with its device token.
2. The Pipecat pipeline in the core finds the end of what you said: Silero
   voice-activity detection, then Smart Turn, which tells a finished sentence
   from a pause.
3. The speech server turns it into text.
4. The voice brain hands the text to the same chat service as the app, in
   voice mode: a fast model and short spoken answers, with the same memory,
   tools and policy engine. If the answer is slow to start, Jarvis says a
   short filler.
5. The answer is spoken sentence by sentence (the speech server again) and
   streamed back as 24 kHz PCM. JSON events carry the transcript and state
   alongside the audio.
6. Talking over Jarvis stops it at once. The part of the answer you heard is
   saved.
7. An action Jarvis just proposed is read back. An exact "confirm" or "cancel"
   approves or rejects it (see [security.md](security.md), "Voice").

```
mic ─► VAD + Smart Turn ─► speech-to-text ─► chat service (voice mode) ─► sentences ─► text-to-speech ─► speaker
        ▲                                                                                                   │
        └─────────────────────────── you talk over it: Jarvis stops and listens ◄───────────────────────────┘
```

**Telegram** (`core/jarvis/telegram/`):

- The core long-polls Telegram's Bot API, so it needs no public address.
- Messages from the linked owner go to the same chat service, with the privacy
  rules for a channel that isn't end-to-end encrypted. Voice notes are
  transcribed by the speech server and answered with a voice note.
- Low- and medium-risk approvals arrive with buttons. Each message is kept in
  step with decisions made elsewhere (`telegram_notices`).

**Onboarding:** each of the 12 modules is a short interview run by an agent.

- It may only write the profile fields of its own module, and every value is
  validated against the profile schema.
- Answers create a new profile version. Old versions are kept.
- **The autonomy gate:** autonomy (L3) stays closed until two things are true:
  - the 20 required fields are at least 80% complete
  - you sign off the profile summary

**Ingestion** (opt-in, one switch per source):

1. A source reads its data: Gmail sent-mail style, calendar patterns, GitHub,
   website, CV or LinkedIn export.
2. Anything from outside is sanitised and wrapped as untrusted.
3. A model extracts *suggestions*. They land on "What I know" > To review for
   you to accept.

## Components (`core/jarvis/`)

| Package | Responsibility |
|---|---|
| `api/` | FastAPI routers, the security middleware (CSP, same-origin checks, no-store), SSE, serving the PWA |
| `auth/` | Passkeys (WebAuthn), setup code, recovery codes, sessions, passkey step-up |
| `policy/` | Action registry, policy config (`policies.yaml`), validators, engine, kill switch and autonomy gate |
| `audit/` | Hash-chained, append-only audit log (IDs and metadata only) |
| `llm/` | Model config (`models.yaml`), the privacy rules, quota tracking, the router with fallback and cooldowns, the test-only fake model |
| `agents/` | The orchestrator agent and the memory-extraction agent |
| `chat/` | Conversations, streaming, context building |
| `memory/` | Facts with provenance and validity periods; hybrid retrieval (pgvector + full-text, rank fusion); local embeddings (fastembed) |
| `profile/` | The versioned profile schema, completeness, sign-off |
| `onboarding/` | The 12 interview modules and their agent |
| `ingestion/` | The consented sources, Google OAuth (PKCE, loopback redirect), style statistics |
| `security/` | Encryption (Fernet), canonical hashing, secret scanners, untrusted-content handling, one-time pairing codes |
| `mail/` | Gmail sync and client, the encrypted mail store, MIME parsing and building, warning signs, triage, drafting, the email actions, who you know, the Inbox, alerts and digests, the triage eval |
| `notify/` | Web Push (VAPID), and telling you directly (push and Telegram) |
| `workflows/` | The executor loop and the scheduled jobs (DBOS) |
| `voice/` | The voice pipeline (Pipecat), the voice brain, voice confirmations, the speech server client, paired devices, the latency benchmark |
| `telegram/` | The Telegram bot: the Bot API client, owner linking, message formatting, approval buttons |
| `doctor.py`, `cli.py` | `jarvis doctor`, `secrets`, `setup-token`, `migrate`, `serve`, `local-models`, `fetch-text-data`, `pull-speech-models`, `bench-voice`, `eval triage` |

**Background work:**

- The executor loop runs every 2 seconds.
- Durable jobs, in your timezone:
  - memory extraction, every 5 minutes
  - housekeeping, hourly: expires stale proposals and removes old
    sessions/challenges
  - nightly maintenance at 03:00, which catches up if the PC was off: purges
    facts you asked to forget, after 7 days, and email text older than 90 days
  - the inbox digests at 07:15 and 17:30 (`config/email.yaml`)
- The email loops, once Google is connected: sync every minute, and sorting
  as mail arrives.
- The Telegram bot's long-poll loop, when a bot token is set.
- At startup, voice checks for its sentence data and downloads it once if
  it's missing.

**Data** (Postgres, migrations in `core/alembic/`):

| Table | Holds |
|---|---|
| `action_proposals` | Proposals and their lifecycle |
| `audit_events` | The hash-chained audit log |
| `facts`, `episodes` | Memory, with embeddings |
| `profile_versions`, `onboarding_modules` | The profile and interview state |
| `conversations`, `chat_messages`, `conversation_turns` | Chat |
| `llm_calls` | Every AI call: model, tokens, latency. Used for quotas and budget |
| `webauthn_credentials`, `auth_sessions`, `auth_challenges`, `recovery_codes` | Sign-in |
| `oauth_tokens` (encrypted), `oauth_pending`, `ingestion_sources` | Sources |
| `push_subscriptions` | Notification endpoints |
| `voice_devices` | Paired satellites: name, token hash, last seen, removed |
| `telegram_notices` | The Telegram messages about approvals, so they stay in step with decisions |
| `mail_messages`, `mail_threads` | Your email and how Jarvis sorted it (content encrypted; addresses, dates and labels readable) |
| `mail_contacts` | Who you've written to and heard from, for "people you know" |
| `mail_drafts` | Jarvis's reply drafts, their send proposals and their Gmail copies |
| `mail_verdicts` | Your "Is this right?" answers, for `make eval` |
| `system_state` | The kill switch, the setup code hash, pairing codes, the Telegram link, the email sync position, the latest digest, and similar |

DBOS keeps its workflow state in its own `dbos` schema in the same database.

## The web app (`web/`)

- **Stack:** React 19, Vite, Tailwind CSS v4, Radix-based components in the
  shadcn style, TanStack Router and Query. It's an installable PWA with an
  offline app shell; the service worker never caches API calls.
- **Pages:**
  - Home
  - Inbox (sorted email, conversations, the reply editor)
  - Chat
  - Talk (voice)
  - Approvals (emails shown as they'll be sent, with your changes to
    Jarvis's draft)
  - What I know (memory and profile)
  - Onboarding
  - Sources
  - Activity (the audit log)
  - Settings (passkeys, notifications, voice, Telegram, models, policies,
    theme)
- **Accessibility and layout:** the end-to-end tests run axe checks on every
  page and check the phone layout at 390 px with no sideways scrolling.

## Configuration

| File | What it controls |
|---|---|
| `.env` | Secrets and addresses (from `.env.example`; `make secrets` fills in the generated ones) |
| `config/models.yaml` | Providers and their privacy flags, models, free-tier limits, which models each task may use, the paid budget |
| `config/policies.yaml` | Autonomy level, risk, validators, undo window and caps for each kind of action; quiet hours |
| `config/voice.yaml` | The speech models and Jarvis's voice, filler timing, and the exact phrases that confirm or cancel an action |
| `config/email.yaml` | How often and how far back to read Gmail, how long to keep email text, automatic drafts, alerts, digest times |
| `config/persona.md` | Jarvis's personality and manners |

## Testing

| Layer | What runs |
|---|---|
| Core unit and integration | pytest against real Postgres + pgvector, never SQLite: policy engine, router, auth (with a software WebAuthn authenticator), memory, onboarding, ingestion, API. Voice runs over a real socket, with recorded speech going through the real turn detection and a fake speech server. Telegram runs against a fake Bot API. Email runs against a fake Gmail with faults (expired sync points, rate limits, revoked access, lost answers after a send) |
| Email injection suite | 61 attack emails through the whole email pipeline and the chat agent, with the normal fake model and an obedient one; 100% must pass (security.md, "Email") |
| Property-based | Hypothesis drives random event sequences through the policy engine (see security.md) |
| Web unit | Vitest + Testing Library |
| End to end | Playwright against a real server and database, with a virtual passkey authenticator. It walks the first-run setup, onboarding, memory, approvals, the kill switch, talking to Jarvis (through Chromium's fake microphone), connecting Gmail and replying with undo (against the fake Gmail), audit integrity, the phone layout and sign-in again, with axe accessibility scans |
| Satellite | pytest on Linux and Windows: the conversation loop against a fake Jarvis that speaks the real protocol (wake word, barge-in, mute, follow-ups, pairing). On Windows also the real wake-word model, the installer's helpers, and the false-wake benchmark on a synthetic room |
| Deployment | CI builds the Docker image, runs `make secrets`, starts the stack and smoke-tests it (health, the PWA, 401 on protected APIs, setup code, doctor, backup) |
| Scripts | shellcheck + shfmt for the WSL scripts; PSScriptAnalyzer (Windows PowerShell 5.1 compatibility) and helper tests on a Windows runner for `setup.ps1` and `install-satellite.ps1` |
| Secrets | gitleaks over the full history |

Model calls in tests use a deterministic fake model (it only exists when
`JARVIS_ALLOW_FAKE_LLM` is set). Tests never call a real AI provider.

## The Windows satellite (`satellite/`)

A small Python tray app with its own locked dependencies, installed per user
by `scripts/windows/install-satellite.ps1`.

- It listens for "Hey Jarvis" on the PC (openWakeWord). Only then does it
  connect, to the same voice socket as the app, with the device token it got
  when paired.
- It's half-duplex: while Jarvis speaks, the microphone isn't sent, but
  "Hey Jarvis" still interrupts.
- After an answer it keeps listening for a follow-up for a few seconds, then
  hangs up.

[satellite/README.md](../satellite/README.md) has the details.

## Decisions

Architecture decisions are recorded in [adr/](adr/).
