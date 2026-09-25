# Security and privacy

Jarvis reads your email, knows your clients and will eventually act on your
behalf. This page covers:

- what it protects
- how it does that
- what's left for you to decide

Every guarantee below is enforced in code and covered by tests. None of them
depend on an AI model behaving well.

## What we defend against

| Threat | Defence |
|---|---|
| **Someone on the internet** | Nothing listens publicly. Every port binds to `127.0.0.1`; your devices reach Jarvis only through Tailscale (WireGuard). The database sits on a Docker network with no internet access and no published port. |
| **Another website or device reaching your voice** | The voice socket only opens for Jarvis's own pages with your session, or for a device you paired. A device's token is stored only as a hash, and removing the device cuts it off. |
| **A stranger on Telegram** | The bot answers only the account you linked, after a passkey tap. Anyone else, and any group, gets no reply at all. |
| **A stolen phone or laptop** | Passkeys (Face ID, fingerprint, Windows Hello) instead of passwords. High-risk approvals need a fresh passkey tap, not just a signed-in session. You can remove a device's passkey and sign out every device (runbook: "Lost access"). |
| **Malicious email and web pages** (prompt injection) | They're treated as untrusted data (next section). An AI can only *propose* actions; code decides what happens. |
| **The AI making a mistake** | Nothing outward-facing happens without your approval unless you loosen a policy yourself. Every action has a preview and a log entry, and sends get a 60-second undo window. |
| **AI providers using your data** | A privacy router that fails closed: personal data only goes to providers that don't train on it, and confidential data only to local or zero-retention ones. |
| **Secrets leaking** | Tokens are encrypted at rest; `.env` is private (mode 600) and gitignored; gitleaks blocks secrets in commits and CI; scanners check outgoing actions. |
| **A compromised dependency** | Lockfiles (`uv.lock`, `pnpm-lock.yaml`), pinned image tags, pnpm blocks install scripts except esbuild, CI checks downloaded tools against pinned SHA-256 hashes. |
| **Losing the PC** | Encrypted backups (Phase 8) and your recovery codes. Turn on BitLocker or Device Encryption, since the database on disk is only as safe as the disk. |

## Nothing acts without passing the policy engine

Agents never call Gmail, Calendar or any other side-effecting API directly.
Everything goes through this pipeline (`core/jarvis/policy/`):

1. **Propose:** an agent calls `propose_action(kind, payload, rationale)`.
   Unknown kinds are refused.
2. **Validate:** deterministic checks run for that kind:
   - leaked-secret scan
   - known recipients
   - "attachment mentioned but missing"
   - link safety
   - opt-out present

   A crashing check counts as a block.
3. **Decide:** `config/policies.yaml` sets each kind's autonomy level:
   - L0 refused
   - L1 draft only
   - L2 needs your approval
   - L3 acts, then tells you

   L3 only applies when all of these hold:
   - the profile is signed off
   - the kill switch is off
   - the daily cap isn't reached
   - every validator passed

   Otherwise the proposal waits for you.
4. **Approve:** your approval is bound to the **SHA-256 of the exact payload
   bytes**. Changing anything means approving again. Approval channels follow
   risk: high and critical risk accept only a passkey tap in the app, no older
   than 2 minutes and used once.
5. **Execute:**
   - The executor runs only approved proposals whose undo window has passed.
   - It re-parses exactly the bytes you approved.
   - It marks the proposal `executing` (committed) before the side effect, so
     a crash leaves `unknown_outcome` for you to check, never a silent retry.
     Execution is at most once.
6. **Audit:** every step is written to a hash-chained, append-only log. Each
   row's hash covers the previous row's hash, so any edit or deletion breaks
   the chain; Activity > **Verify integrity** checks it. The log holds IDs and
   metadata, never email bodies or chat text.

Some rules can't be configured away. Jarvis refuses to start if
`policies.yaml` breaks them:

- money, deletions and production deploys can never be L3
- high or critical risk can never be L3, and needs the passkey channel

A property-based test (`core/tests/integration/test_policy_properties.py`)
runs 120 random sequences of up to 40 steps each. The steps are drawn from:

- proposals
- approvals over every channel
- rejections and cancellations
- kill-switch and gate toggles
- clock jumps
- direct database tampering

After every executor run it checks five invariants:

1. Nothing runs twice.
2. Nothing runs while the kill switch is on.
3. Nothing runs before its undo window ends.
4. Everything that ran was approved with the hash of exactly its bytes, over
   an allowed channel, or was auto-approved at L3.
5. Tampered proposals never run.

**Kill switch:** Home > **Stand down**, or Settings. While it's on, nothing
executes, including actions you already approved. It's stored in the
database, so it survives restarts.

## Untrusted content (prompt-injection defence)

Email bodies, web pages, documents and anything else from outside
(`core/jarvis/security/untrusted.py`) get this treatment:

- **Sanitised:**
  - invisible characters and Unicode "tag" characters (used to hide
    instructions) are stripped
  - HTML comments are removed
  - length is capped
- **Wrapped** in `<untrusted>` markers with a random per-call nonce. The
  system prompt tells the model that nothing inside is an instruction.
- **Flagged** when it looks like an injection attempt ("ignore previous
  instructions", fake system prompts, requests to send data elsewhere). The
  warning is attached to the wrapped text, so the model sees it too.

The structural defence matters more than the model's obedience:

- Agents that read untrusted content get **no side-effect tools**.
- Anything they suggest still goes through the policy engine above.
- The app never loads remote images or links from email: its Content Security
  Policy (`img-src 'self' data: blob:`) blocks them.

Phase 3 adds a red-team suite of attack emails that must pass at 100%, meaning
zero unauthorised actions.

## Which AI provider sees what

Every AI call names a task, and every task has a data class. The router
(`core/jarvis/llm/router.py`) only tries models whose provider may receive
that class, and errors if none is left. It never quietly falls back to
somewhere it shouldn't.

| Provider | Trains on your data? | Zero retention? | May receive |
|---|---|---|---|
| Ollama (your GPU) | No, it's local | Yes | Everything |
| Groq (free tier) | No (Groq's terms, free tier included) | Yes, **once you turn it on** in the Groq console | Public, personal, confidential |
| Gemini API (free tier) | **Yes**, and people may review it | No | Public only |
| OpenRouter free models | May log for training | No | Public only |
| Anthropic / OpenAI (paid; off until you set a budget) | No (API terms) | Not by default | Public, personal |

- **Tasks:**
  - Chat, voice, onboarding, memory extraction, ingestion, triage and drafting
    are **personal**.
  - Client work under NDA is **confidential**.
  - News and research are **public**.
- **Changing providers:** edit these flags in `config/models.yaml`.
- **Tests:**
  - every shipped task is checked against these rules
  - the router must refuse a task when only disallowed providers are left
- **Groq's zero retention:** `config/models.yaml` marks Groq as zero-retention
  because you're asked to switch that on. `make doctor` reminds you to check.

Traces of AI calls, which include prompts and replies, go only to the Phoenix
container on your PC. Its own usage analytics are turned off.

## Sign-in and sessions

- **Passkeys only** (WebAuthn, with user verification required). The first
  passkey needs a one-time setup code:
  - It's printed in the server log, valid for 24 hours, and stored only as a
    hash.
  - It's accepted only while no passkey exists.
- **10 recovery codes:**
  - They're shown once and stored as hashes; each works once.
  - After 10 failed attempts in 15 minutes, recovery is locked.
- **The session cookie:**
  - `HttpOnly`, `SameSite=Strict`, and `Secure` over HTTPS.
  - Only its hash is stored.
  - It expires after 30 days (`JARVIS_SESSION_TTL_HOURS`).
- **Request protections:**
  - State-changing API requests must come from Jarvis's own origin.
  - API responses are never cached.
  - A strict Content Security Policy, `frame-ancestors 'none'` and
    `Cross-Origin-Opener-Policy`.
- **Your Windows password** (for the boot task) is stored by Windows Task
  Scheduler, not by Jarvis. Use `-NoStoredPassword` if you'd rather not;
  docs/setup.md explains the trade-off.

## Voice

- **Audio stays on the PC.** Speech-to-text and text-to-speech run in the
  `speech` container:
  - It has no published port, and it only answers requests that carry
    `SPEECH_API_KEY`.
  - Its logs, and the voice pipeline's, only record warnings, because their
    debug output would include what was said.
- **The voice socket** (`/api/voice/ws`) accepts two kinds of caller:
  - **The app**, with your session cookie. Jarvis checks the page's origin, so
    a page on another site can't open the socket (cross-site WebSocket
    hijacking).
  - **A paired device**, with its token. Each token is random (256 bits),
    shown to the device once, and stored only as a SHA-256 hash. Removing the
    device in Settings > Voice cuts it off.

  At most two voice sessions run at once.
- **Pairing a device** needs a code that you make in the app, after a passkey
  tap. The code:
  - works once, and expires after 10 minutes
  - is stored only as a hash
  - locks after 10 wrong tries in 15 minutes

  Linking Telegram uses the same rules.
- **The PC's tray app** (the satellite):
  - Until it hears "Hey Jarvis", nothing leaves the PC and no connection is
    open. The wake word is detected on the PC (openWakeWord), with models
    checked against pinned SHA-256 hashes.
  - While Jarvis speaks, the microphone isn't sent, so Jarvis never hears
    itself.
  - Muting ignores the microphone completely and hangs up. Nothing is sent
    after you mute, not even what it kept while connecting.
  - Its token lives in Windows Credential Manager and is never logged.
- **Approving by voice:**
  - Only risk levels whose `approval_channels` include `voice` can be approved
    by voice: low and medium by default. High and critical never can: Jarvis
    refuses to start if `policies.yaml` tries to allow it.
  - Jarvis reads the action back and waits for an exact phrase from
    `config/voice.yaml`, such as "confirm" or "cancel". Speech recognition
    mishears, and a TV can talk, so casual words like "yeah" never approve
    anything; tests check this. Anything else counts as a new request, and
    the action keeps waiting in Approvals.
  - The confirmation is bound to the hash of the action that was read out,
    and expires after 60 seconds.
  - "Jarvis, stand down" turns the kill switch on without asking, because it
    can only make Jarvis do less.

## Telegram

- **One owner.** The bot answers only the Telegram account you link, with a
  code made in the app after a passkey tap. Anyone else, and any group, gets no
  reply at all, not even an error.
- **Telegram chats aren't end-to-end encrypted,** so Telegram's servers can
  read them. In Telegram conversations:
  - The model doesn't see your sensitive memories, summaries of past
    conversations, or the personal section of your profile, so it can't repeat
    them. It's told to keep secrets, money, health and family details for the
    app, along with any topics you listed as sensitive.
  - Anything that looks like a secret (keys, tokens, passwords) is masked
    before a message leaves Jarvis.
- **Forwarded messages** are someone else's words. Jarvis reads them as
  untrusted information, never as instructions.
- **Buttons are bound to the action.**
  - Approve and Reject carry the action's ID and the start of its payload
    hash, and go through the same policy engine as the app.
  - They only appear for the risk levels `policies.yaml` allows on Telegram:
    low and medium by default, never high or critical.
- **The bot token** controls the bot. It lives in `.env`, and Jarvis strips it
  from its HTTP logs and error messages. If it leaks, revoke it in @BotFather
  (runbook: "Rotating secrets").
- **No open port.** Jarvis fetches its messages from Telegram (long polling),
  so nothing on the internet can reach it.

## Secrets and encryption

- `.env` holds every secret. It's gitignored, created with mode 600, and never
  baked into images: `.dockerignore` excludes it.
- OAuth tokens (Google) are encrypted with `JARVIS_SECRET_KEY` (Fernet:
  AES-128-CBC + HMAC-SHA256). Keep a copy of that key apart from your backups.
- `SPEECH_API_KEY` is generated by `make secrets`, and only Jarvis and the
  speech server know it. `TELEGRAM_BOT_TOKEN` comes from @BotFather.
- Only read-only Google scopes are requested for now (`gmail.readonly`,
  `calendar.readonly`). Sending arrives in Phase 3, behind approvals.
- Tests strip every credential from the environment, so a test can never call
  a real service with a real key.
- gitleaks runs in pre-commit and in CI, over the full history. Its only
  exception is one fake key in the tests, limited to that exact value in that
  folder.

## What leaves your PC

- **AI calls:** only to the providers in the table above, only with the data
  classes allowed, and only when you've added their keys.
- **Google API calls:** to read your sent mail statistics and calendar, only
  after you connect Google and switch those sources on.
- **Web and GitHub reads:** when you run the website or GitHub sources.
- **Web push:** notifications travel through your browser maker's push
  service, encrypted end to end (Web Push encryption), so that service can't
  read them.
- **Telegram:** only if you set it up. Your messages to the bot, and its
  replies, go through Telegram's servers (see "Telegram" above for what's
  kept out).
- **Your voice:** never, except voice notes you send on Telegram. Speech is
  handled on the PC.
- **Downloads:** updates and models come from their official sources, when you
  install, `make update` or `make pull-models`:
  - Docker images, and Python and npm packages
  - Ollama models, the embedding model and the speech models (Hugging Face)
  - the wake-word models and the voice sentence data, checked against pinned
    SHA-256 hashes
- **Nothing else.** Jarvis has no telemetry, analytics or crash reporting.

## Residual risks, and what you can do

- **The PC is the vault.** Anyone with admin access to it can read the
  database. Use BitLocker or Device Encryption, a strong Windows password and
  automatic updates.
- **Free-tier terms change.** Re-check the provider table when you add keys,
  and keep `make doctor ONLINE=1` in your routine.
- **Voice doesn't know who's speaking.** Anyone in the room can say
  "Hey Jarvis", ask things, and say "confirm" while a low- or medium-risk
  action waits. Mute the PC's tray app when you have visitors. Or remove
  `voice` from `approval_channels` in `policies.yaml`, and approve in the app
  instead. Checking that it's your voice (speaker verification) is a possible
  later addition.
- **Outreach law.** Before the lead engine (Phase 5) sends anything, have a
  Kenyan data-protection lawyer confirm the approach under the Data Protection
  Act 2019. The built-in opt-out and suppression features support compliance
  but aren't legal advice.
- **Signing out other devices** is a database command today (runbook). A
  button for it is planned for Phase 8.
