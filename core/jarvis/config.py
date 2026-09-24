"""Runtime settings, read from environment variables (see `.env.example`)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_config_dir() -> Path:
    return REPO_ROOT / "config"


def _default_data_dir() -> Path:
    return REPO_ROOT / "data"


class Settings(BaseSettings):
    """Process-wide settings. Construct via `get_settings()` in app code."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    env: Literal["production", "development", "test"] = Field(
        default="production", alias="JARVIS_ENV"
    )
    timezone: str = Field(default="Africa/Nairobi", alias="JARVIS_TIMEZONE")
    public_origin: str = Field(default="http://localhost:8080", alias="JARVIS_PUBLIC_ORIGIN")
    secret_key: SecretStr | None = Field(default=None, alias="JARVIS_SECRET_KEY")
    session_ttl_hours: int = Field(default=720, alias="JARVIS_SESSION_TTL_HOURS", ge=1)
    database_url: str = Field(
        default="postgresql+asyncpg://jarvis:jarvis@localhost:5432/jarvis", alias="DATABASE_URL"
    )
    config_dir: Path = Field(default_factory=_default_config_dir, alias="JARVIS_CONFIG_DIR")
    data_dir: Path = Field(default_factory=_default_data_dir, alias="JARVIS_DATA_DIR")
    web_dist_dir: Path | None = Field(default=None, alias="JARVIS_WEB_DIST")
    models_file: Path | None = Field(default=None, alias="JARVIS_MODELS_FILE")
    voice_file: Path | None = Field(default=None, alias="JARVIS_VOICE_FILE")
    allow_fake_llm: bool = Field(default=False, alias="JARVIS_ALLOW_FAKE_LLM")
    enable_scheduler: bool = Field(default=True, alias="JARVIS_ENABLE_SCHEDULER")
    auto_migrate: bool = Field(default=True, alias="JARVIS_AUTO_MIGRATE")
    host: str = Field(default="0.0.0.0", alias="JARVIS_HOST")  # noqa: S104 (inside the container)
    port: int = Field(default=8080, alias="JARVIS_PORT")
    log_level: str = Field(default="INFO", alias="JARVIS_LOG_LEVEL")
    embedder: Literal["fastembed", "hash"] = Field(default="fastembed", alias="JARVIS_EMBEDDER")

    google_client_id: str | None = Field(default=None, alias="GOOGLE_OAUTH_CLIENT_ID")
    google_client_secret: SecretStr | None = Field(default=None, alias="GOOGLE_OAUTH_CLIENT_SECRET")
    google_redirect_uri: str = Field(
        default="http://127.0.0.1:8080/api/integrations/google/callback",
        alias="GOOGLE_OAUTH_REDIRECT_URI",
    )
    github_token: SecretStr | None = Field(default=None, alias="GITHUB_TOKEN")

    vapid_public_key: str | None = Field(default=None, alias="VAPID_PUBLIC_KEY")
    vapid_private_key: SecretStr | None = Field(default=None, alias="VAPID_PRIVATE_KEY")
    vapid_subject: str = Field(default="mailto:owner@example.com", alias="VAPID_SUBJECT")

    phoenix_endpoint: str | None = Field(default=None, alias="PHOENIX_COLLECTOR_ENDPOINT")

    # Local speech server (speaches: faster-whisper + Kokoro), OpenAI-compatible.
    speech_base_url: str = Field(default="http://speech:8000/v1", alias="SPEECH_BASE_URL")
    speech_api_key: SecretStr | None = Field(default=None, alias="SPEECH_API_KEY")

    telegram_bot_token: SecretStr | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")

    @field_validator("public_origin")
    @classmethod
    def _strip_origin(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("JARVIS_PUBLIC_ORIGIN must look like https://host[:port]")
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def voice_config_path(self) -> Path:
        return self.voice_file or self.config_dir / "voice.yaml"

    @property
    def models_config_path(self) -> Path:
        return self.models_file or self.config_dir / "models.yaml"

    @property
    def rp_id(self) -> str:
        """WebAuthn relying-party ID: the hostname passkeys are bound to."""
        host = urlparse(self.public_origin).hostname
        assert host is not None
        return host

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def sync_database_url(self) -> str:
        """A psycopg (sync) URL for tools that need one, like DBOS and Alembic."""
        url = self.database_url
        for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql://" + url[len(prefix) :]
        return url

    @property
    def secure_cookies(self) -> bool:
        return self.public_origin.startswith("https://")

    def provider_env(self) -> dict[str, str]:
        """Environment snapshot used to look up provider API keys by name."""
        return dict(os.environ)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
