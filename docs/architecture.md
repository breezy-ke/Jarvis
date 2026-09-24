# Architecture

Jarvis is a single-user assistant that runs on your own PC. It has two parts:

- **core**: a Python API that holds the agents, memory, policy engine and
  model router.
- **web**: a React app (PWA) served by the core.

Everything stateful lives in one Postgres database.

```
  Phone / laptop browser (PWA)                       Windows PC
          │  HTTPS over Tailscale (WireGuard)         ┌───────────────────────────────────────────┐
          └──────────────────────────────────────────►│ tailscale serve :443 ─► 127.0.0.1:8080    │
                                                      │                                           │
                                                      │ WSL2 Ubuntu · Docker Engine (+GPU)        │
                                                      │ ┌───────────────────────────────────────┐ │
                                                      │ │ core (FastAPI, :8080)                 │ │
                                                      │ │   API + built PWA + background work   │ │
                                                      │ └──┬───────────────┬──────────────┬─────┘ │
                                                      │    │ internal net  │              │       │
                                                      │ ┌──▼──────────┐ ┌──▼─────────┐ ┌──▼─────┐ │
                                                      │ │ postgres 17 │ │ ollama     │ │ phoenix│ │
                                                      │ │ + pgvector  │ │ (GPU)      │ │ traces │ │
                                                      │ └─────────────┘ └────────────┘ └────────┘ │
                                                      └───────────────────────────────────────────┘
   Cloud AI (only the data each is allowed): Groq · Gemini · OpenRouter · (paid, off by default)
```

- **Ports:** every port binds to `127.0.0.1`. Postgres has no published port
  and sits on an `internal` Docker network with no internet access.
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
   - `propose_action`, the only way to affect anything outside Jarvis
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
| `security/` | Encryption (Fernet), canonical hashing, secret scanners, untrusted-content handling |
| `notify/` | Web Push (VAPID) |
| `workflows/` | The executor loop and the scheduled jobs (DBOS) |
| `doctor.py`, `cli.py` | `jarvis doctor`, `secrets`, `setup-token`, `migrate`, `serve`, `local-models` |

**Background work:**

- The executor loop runs every 2 seconds.
- Durable jobs, in your timezone:
  - memory extraction, every 5 minutes
  - housekeeping, hourly: expires stale proposals and removes old
    sessions/challenges
  - nightly maintenance at 03:00, which catches up if the PC was off: purges
    facts you asked to forget, after 7 days

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
| `system_state` | The kill switch, the setup code hash, and similar |

DBOS keeps its workflow state in its own `dbos` schema in the same database.

## The web app (`web/`)

- **Stack:** React 19, Vite, Tailwind CSS v4, Radix-based components in the
  shadcn style, TanStack Router and Query. It's an installable PWA with an
  offline app shell; the service worker never caches API calls.
- **Pages:**
  - Home
  - Chat
  - Approvals
  - What I know (memory and profile)
  - Onboarding
  - Sources
  - Activity (the audit log)
  - Settings (passkeys, notifications, models, policies, theme)
- **Accessibility and layout:** the end-to-end tests run axe checks on every
  page and check the phone layout at 390 px with no sideways scrolling.

## Configuration

| File | What it controls |
|---|---|
| `.env` | Secrets and addresses (from `.env.example`; `make secrets` fills in the generated ones) |
| `config/models.yaml` | Providers and their privacy flags, models, free-tier limits, which models each task may use, the paid budget |
| `config/policies.yaml` | Autonomy level, risk, validators, undo window and caps for each kind of action; quiet hours |
| `config/persona.md` | Jarvis's voice and manners |

## Testing

| Layer | What runs |
|---|---|
| Core unit and integration | pytest against real Postgres + pgvector, never SQLite: policy engine, router, auth (with a software WebAuthn authenticator), memory, onboarding, ingestion, API |
| Property-based | Hypothesis drives random event sequences through the policy engine (see security.md) |
| Web unit | Vitest + Testing Library |
| End to end | Playwright against a real server and database, with a virtual passkey authenticator. It walks the first-run setup, onboarding, memory, approvals, the kill switch, audit integrity, the phone layout and sign-in again, with axe accessibility scans |
| Deployment | CI builds the Docker image, runs `make secrets`, starts the stack and smoke-tests it (health, the PWA, 401 on protected APIs, setup code, doctor, backup) |
| Scripts | shellcheck + shfmt for the WSL scripts; PSScriptAnalyzer (Windows PowerShell 5.1 compatibility) and helper tests on a Windows runner for `setup.ps1` |
| Secrets | gitleaks over the full history |

Model calls in tests use a deterministic fake model (it only exists when
`JARVIS_ALLOW_FAKE_LLM` is set). Tests never call a real AI provider.

## Decisions

Architecture decisions are recorded in [adr/](adr/).
