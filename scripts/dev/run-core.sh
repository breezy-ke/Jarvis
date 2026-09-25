#!/usr/bin/env bash
# Runs the core for development against the dev database (make dev-db).
# Pair it with `make dev-web` and open http://localhost:5173.
# Any variable you export first wins, e.g. to develop without AI models:
#   JARVIS_ALLOW_FAKE_LLM=1 JARVIS_MODELS_FILE=core/tests/fixtures/models.fake.yaml make dev-core
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

mkdir -p data
key_file=data/dev-secret.key
if [[ ! -s "$key_file" ]]; then
  (umask 077 && head -c 32 /dev/urandom | base64 | tr '+/' '-_' >"$key_file")
fi

export JARVIS_ENV="${JARVIS_ENV:-development}"
export JARVIS_SECRET_KEY="${JARVIS_SECRET_KEY:-$(cat "$key_file")}"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis_dev}"
# The Vite dev server's address: passkeys and the same-origin check use it.
export JARVIS_PUBLIC_ORIGIN="${JARVIS_PUBLIC_ORIGIN:-http://localhost:5173}"
export JARVIS_HOST="${JARVIS_HOST:-127.0.0.1}"
# The voice pipeline's sentence data, as `make install` downloads it.
export NLTK_DATA="${NLTK_DATA:-$PWD/data/nltk_data}"
export PYDANTIC_AI_NO_BANNER=1
if [[ -n "${JARVIS_MODELS_FILE:-}" && "$JARVIS_MODELS_FILE" != /* ]]; then
  JARVIS_MODELS_FILE="$PWD/$JARVIS_MODELS_FILE"
  export JARVIS_MODELS_FILE
fi

cd core
exec uv run jarvis serve
