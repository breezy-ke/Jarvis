# Architecture decision records

Short records of the decisions that shape Jarvis: the context, what we chose,
and what it costs. A new decision gets the next number. A reversed decision
isn't deleted: it's marked superseded, with a link to the one that replaced it.

| # | Decision |
|---|---|
| [0001](0001-own-core-not-a-fork.md) | Build a small core of our own, not a fork of an agent platform |
| [0002](0002-postgres-is-the-only-state.md) | Postgres (with pgvector and DBOS) holds all state |
| [0003](0003-agents-propose-code-decides.md) | Agents only propose; a deterministic policy engine decides; approvals are hash-pinned |
| [0004](0004-privacy-router-fails-closed.md) | Every AI call goes through a privacy router that fails closed |
| [0005](0005-windows-wsl2-docker-engine.md) | Run on the owner's Windows PC: WSL2 + Docker Engine, a boot task, no Docker Desktop |
| [0006](0006-passkeys-over-tailscale.md) | Passkeys for sign-in; reach the PC only over Tailscale |
| [0007](0007-voice-in-the-core-over-a-websocket.md) | Voice runs in the core, over one WebSocket, with a local speech server |
| [0008](0008-telegram-by-long-polling.md) | Telegram through the raw Bot API, by long polling |
| [0009](0009-gmail-by-rest-polling-and-recipients-by-code.md) | Gmail by REST and polling; mail agents without tools; recipients by code |
