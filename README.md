# Jarvis

A private AI assistant that runs on your own PC, learns who you are before it
acts, and never does anything outward-facing without your say-so.

Built for one person: an IT consultant and full-stack developer running a
digital-craft consultancy. It's free to run (local AI on your GPU plus free
cloud tiers), and it's reachable from your phone over private HTTPS.

## What it does

| | Status |
|---|---|
| **Knows you first:** a 12-part onboarding interview, a versioned profile, long-term memory you can see, edit and delete, and opt-in learning from your sent mail style, calendar, GitHub, website and CV | ✅ Phase 1 |
| **Safe by construction:** agents only *propose*; a policy engine decides; approvals are pinned to the exact payload; undo windows; kill switch; tamper-evident activity log | ✅ Phase 0 |
| **Private by default:** every AI call is routed by data class, and personal data never reaches a provider that trains on it | ✅ Phase 0 |
| **Your app:** installable on phone and desktop, passkey sign-in, push notifications, dark and light themes, accessible | ✅ Phase 0 |
| **Voice:** talk to Jarvis in the app and interrupt it; "Hey Jarvis" on your PC; spoken replies; approve everyday actions by saying "confirm". Speech runs on your PC | ✅ Phase 2 |
| **Telegram:** chat by text or voice note, and approve everyday actions with a tap | ✅ Phase 2 |
| **Email:** triage, summaries, replies drafted in your style, sending with approval and a 60-second undo | Phase 3 |
| **Daily tech brief and research**, with sources and "why it matters to you" | Phase 4 |
| **Lead engine:** East African SMEs, international startups, tenders, agencies needing overflow | Phase 5 |
| **UI Studio:** websites and apps (Next.js, Nuxt, WordPress, Laravel, Flutter, React Native) with automated design, accessibility and performance gates | Phase 6 |
| **Calendar, tasks, proposals and invoices, a dev copilot, content** | Phase 7 |

## Get started

1. **On Windows:** follow [docs/setup.md](docs/setup.md). One PowerShell script
   sets up WSL2, Docker with your GPU, Tailscale and a boot task, then starts
   Jarvis.
2. **Sign in:** open the address it prints, enter the setup code and create
   your passkey.
3. **Onboard:** do the interview. Jarvis stays in "talk and draft" mode until
   you sign off your profile.
4. **Talk:** open the Talk page, or set up "Hey Jarvis" on the PC and Telegram
   (setup steps 6 and 7).

Day to day, run these in Ubuntu from `~/Jarvis`:

```bash
make doctor     # checks everything and explains any fixes
make logs       # what's happening
make update     # latest version
make backup     # database backup
```

`make` on its own lists every command. The [runbook](docs/runbook.md) covers
updates, backups, the kill switch, lost devices and troubleshooting.

## How it's built

- **core:** Python (FastAPI, Pydantic AI, SQLAlchemy, DBOS) on Postgres with
  pgvector. Voice uses Pipecat.
- **web:** a React PWA (Vite, Tailwind CSS, Radix, TanStack).
- **satellite:** the "Hey Jarvis" Windows tray app (Python, openWakeWord).
- **Deployment:** Docker Compose, with Ollama for local models, a local speech
  server (speaches: faster-whisper and Kokoro), and Phoenix for local AI
  traces.

| Doc | For |
|---|---|
| [docs/setup.md](docs/setup.md) | Installing on your PC, step by step |
| [docs/runbook.md](docs/runbook.md) | Running it: commands, updates, backups, fixes, checks per phase |
| [docs/security.md](docs/security.md) | Threat model, approvals, which AI provider sees what, secrets |
| [docs/architecture.md](docs/architecture.md) | Components, request flows, data, testing |
| [docs/adr/](docs/adr/) | Why it's built this way |

## Development

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22 with pnpm
10, and Docker for the test database.

```bash
make install    # Python (core and satellite) and web dependencies
make dev-db     # throwaway Postgres + pgvector on 127.0.0.1:5432
make check      # lint + types + unit/integration tests (what CI runs)
make e2e        # browser end-to-end tests (Playwright)
```

Run the app locally with hot reload (two terminals), then open
http://localhost:5173:

```bash
make dev-core   # the API on :8080, using the jarvis_dev database
make dev-web    # the web app, proxying /api to the core
```

To work on the UI without any AI models, start the core with the test fake:
`JARVIS_ALLOW_FAKE_LLM=1 JARVIS_MODELS_FILE=core/tests/fixtures/models.fake.yaml make dev-core`.

Set up the git hooks once with `uv tool install pre-commit && pre-commit install`.
They run ruff, pyright, eslint, prettier, tsc, shellcheck, shfmt and gitleaks
before each commit.

**Ground rules:**

- **Never commit personal data or secrets.** `data/` and `.env` are
  gitignored, and gitleaks runs in the hooks and in CI.
- **Tests use synthetic data only.** They never call real AI providers or
  Google.
- **A new capability** needs a policy entry in `config/policies.yaml`, tests,
  and a line in the runbook's per-phase checks.
