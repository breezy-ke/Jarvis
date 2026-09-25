# 0008: Telegram through the raw Bot API, by long polling

- Status: accepted
- Date: 2026-09-25

## Context

- **The need:** the owner wants to reach Jarvis from a messaging app, by text
  and voice note, and approve everyday actions without opening the app.
- **WhatsApp is out:** its business platform needs Meta verification and
  charges per message. Telegram bots are free.
- **No public address:** Jarvis is reachable only over Tailscale (ADR 0006).
  Webhooks would need an address Telegram can reach.
- **Telegram isn't end-to-end encrypted:** its servers can read bot chats.
- **The bot token is a credential:** whoever has it controls the bot.

## Decision

- **Long polling:** the core calls `getUpdates` in a loop, waiting up to 25 s
  for each batch. It needs no inbound port or public address.
- **The raw Bot API over httpx**, not a bot framework. Jarvis uses about a
  dozen methods, and owning the client keeps the dependencies small. It also
  keeps control over where the token could appear: it's stripped from HTTP
  logs and error messages.
- **One owner:**
  - The owner links their account with a one-time code, made in the app
    after a passkey tap (`/start CODE`).
  - Everyone else, and every group, is ignored without a reply.
- **Approvals with buttons** only for the risk levels `policies.yaml` allows
  on Telegram (low and medium by default). A button carries the action's ID
  and the start of its payload hash, and goes through the same policy engine
  as the app. The messages are kept in step with decisions made elsewhere.
- **Privacy on an unencrypted channel:**
  - Sensitive memories, past-conversation summaries and the personal profile
    stay out of the model's context.
  - Secret-looking values are masked before sending.
  - Forwarded messages are treated as untrusted content.

## Consequences

- **Good:** no open port, free, and replies usually arrive within a second
  or two.
- **Costs:**
  - Telegram sees what the owner writes there. The redaction limits what
    Jarvis writes back, not what the owner sends.
  - Long polling keeps one HTTPS request open to Telegram at all times, which
    is cheap.
  - One linked account only. Linking a new one disconnects the old one, and
    tells it so.
  - If the token leaks, the owner revokes it in @BotFather. The link survives,
    because it's still the same bot.
