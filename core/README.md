# jarvis-core

The Jarvis API: agents, memory, profile, the policy engine and the privacy-aware
model router. It also serves the built PWA from `web/dist`.

```bash
uv sync --all-extras          # install (extras: local-ml = fastembed, tracing = OpenTelemetry)
uv run pytest                 # tests; needs Postgres + pgvector (make dev-db from the repo root)
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run jarvis --help          # serve | migrate | setup-token | secrets | doctor | local-models
```

Tests use `JARVIS_TEST_DATABASE_URL`, which defaults to
`postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis_test`. They wipe that
database.

See [docs/architecture.md](../docs/architecture.md) for how the packages fit
together.
