# syntax=docker/dockerfile:1.7
# One image: the core API (Python) serving the built PWA.

# --- 1. Build the PWA ------------------------------------------------------------
FROM node:22-bookworm-slim AS web
WORKDIR /web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm exec vite build

# --- 2. Core runtime ---------------------------------------------------------------
FROM python:3.12-slim-bookworm AS core
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYDANTIC_AI_NO_BANNER=1 \
    FASTEMBED_CACHE_PATH=/app/models

RUN groupadd --gid 1000 jarvis && useradd --uid 1000 --gid 1000 --create-home jarvis
WORKDIR /app/core

# Dependencies first (cached layer), then the project itself.
COPY core/pyproject.toml core/uv.lock core/README.md ./
RUN uv sync --frozen --no-dev --all-extras --no-install-project
COPY core/ ./
RUN uv sync --frozen --no-dev --all-extras

COPY config/ /app/config/
COPY --from=web /web/dist /app/web/dist

# Pre-fetch the local embedding model (~130 MB) so memory search works offline.
# If the build machine has no internet, it downloads on first use instead.
RUN mkdir -p /app/models /data \
 && (/app/core/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" \
     || echo "embedding model will download on first use") \
 && chown -R jarvis:jarvis /app/models /data

# Sentence data for the voice pipeline (NLTK punkt_tab, checksum-pinned). Without
# internet at build time, Jarvis downloads it once at startup instead.
RUN (/app/core/.venv/bin/jarvis fetch-text-data --dest /app/nltk_data \
     || echo "sentence data will download on first start") \
 && mkdir -p /app/nltk_data && chown -R jarvis:jarvis /app/nltk_data

ENV PATH="/app/core/.venv/bin:$PATH" \
    JARVIS_CONFIG_DIR=/app/config \
    JARVIS_WEB_DIST=/app/web/dist \
    JARVIS_DATA_DIR=/data \
    NLTK_DATA=/app/nltk_data

USER jarvis
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"
CMD ["jarvis", "serve"]
