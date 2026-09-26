"""`jarvis doctor`: check the setup and say exactly how to fix what's missing."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import text

from jarvis.clock import Clock, SystemClock
from jarvis.config import Settings, get_settings
from jarvis.ingestion.google import GoogleConnection
from jarvis.llm.config import ModelsConfig, PrivacyClass, load_models_config
from jarvis.llm.privacy import allowed
from jarvis.mail.actions import JARVIS_LABELS
from jarvis.mail.config import EmailConfigError, load_email_config
from jarvis.mail.sync import SyncState
from jarvis.policy.config import load_policies
from jarvis.voice.config import VoiceConfigError, load_voice_config
from jarvis.voice.speech import SpeechClient
from jarvis.voice.textdata import punkt_available

OK, WARN, FAIL = "ok", "warn", "fail"
_ICONS = {OK: "✔", WARN: "⚠", FAIL: "✘"}


@dataclass(frozen=True)
class Check:
    status: str
    title: str
    detail: str = ""
    fix: str = ""


def local_model_for_vram(vram_gb: float) -> str:
    """Pick a local model that fits next to the voice models (about 2 GB)."""
    if vram_gb >= 24:
        return "qwen3:30b-a3b"
    if vram_gb >= 16:
        return "qwen3:14b"
    if vram_gb >= 10:
        return "qwen3:8b"
    return "qwen3:4b"


def gpu_vram_gb() -> float | None:
    # The core container can't see the GPU (only Ollama gets it), so `make doctor`
    # measures VRAM on the host and passes it in.
    override = os.environ.get("JARVIS_GPU_VRAM_GB", "").strip()
    if override:
        try:
            return float(override) if float(override) > 0 else None
        except ValueError:
            pass
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        return max(float(v) for v in out.split()) / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def check_settings(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    if settings.secret_key is None:
        checks.append(
            Check(FAIL, "Encryption key", "JARVIS_SECRET_KEY is empty", "Run `make secrets`.")
        )
    else:
        from jarvis.security.crypto import Vault, VaultError

        try:
            Vault(settings.secret_key.get_secret_value())
            checks.append(Check(OK, "Encryption key", "JARVIS_SECRET_KEY is valid"))
        except VaultError as exc:
            checks.append(Check(FAIL, "Encryption key", str(exc), "Run `make secrets`."))
    origin = urlparse(settings.public_origin)
    if origin.scheme != "https" and origin.hostname not in {"localhost", "127.0.0.1"}:
        checks.append(
            Check(
                FAIL,
                "Public address",
                f"{settings.public_origin} is not HTTPS: the mic and passkeys won't work",
                "Run `tailscale serve --bg 8080` on the PC and set JARVIS_PUBLIC_ORIGIN to the "
                "https://<pc>.<tailnet>.ts.net address it prints.",
            )
        )
    else:
        checks.append(Check(OK, "Public address", settings.public_origin))
    if settings.vapid_public_key and settings.vapid_private_key:
        checks.append(Check(OK, "Push notifications", "VAPID keys present"))
    else:
        checks.append(
            Check(WARN, "Push notifications", "VAPID keys missing", "Run `make secrets`.")
        )
    if settings.google_client_id and settings.google_client_secret:
        checks.append(Check(OK, "Google OAuth client", "configured"))
    else:
        checks.append(
            Check(
                WARN,
                "Google OAuth client",
                "not configured (needed for Gmail/Calendar)",
                "Create a Desktop OAuth client in Google Cloud Console (docs/setup.md, step 3).",
            )
        )
    return checks


def check_configs(settings: Settings) -> tuple[list[Check], ModelsConfig | None]:
    checks: list[Check] = []
    try:
        load_policies(settings.config_dir / "policies.yaml")
        checks.append(Check(OK, "policies.yaml", "valid and safe"))
    except ValueError as exc:
        checks.append(
            Check(FAIL, "policies.yaml", str(exc), "Fix the file; Jarvis refuses to start with it.")
        )
    try:
        models = load_models_config(settings.models_file or settings.config_dir / "models.yaml")
        checks.append(
            Check(OK, "models.yaml", f"{len(models.models)} models, {len(models.tasks)} tasks")
        )
        return checks, models
    except ValueError as exc:
        checks.append(Check(FAIL, "models.yaml", str(exc), "Fix the file."))
        return checks, None


def _task_fix(models: ModelsConfig, candidates: tuple[str, ...], privacy: PrivacyClass) -> str:
    """How to give a task a usable model, from the candidates it may use."""
    fixes: list[str] = []
    for ref in candidates:
        provider = models.providers[models.models[ref].provider]
        if not allowed(provider, privacy) or provider.paid:
            continue
        if provider.local:
            fix = "start Ollama (`make up`, then `make pull-models`)"
        elif provider.api_key_env:
            fix = f"add {provider.api_key_env} to .env"
        else:
            continue
        if fix not in fixes:
            fixes.append(fix)
    if not fixes:
        return "Add an allowed model for this task in config/models.yaml."
    if len(fixes) == 1:
        return fixes[0][0].upper() + fixes[0][1:] + "."
    return "Either " + ", or ".join(fixes) + "."


def check_privacy(models: ModelsConfig, env: Mapping[str, str]) -> list[Check]:
    checks: list[Check] = []
    for name, task in models.tasks.items():
        usable = []
        for ref in task.candidates:
            model = models.models[ref]
            provider = models.providers[model.provider]
            key_ok = (
                provider.local or not provider.api_key_env or bool(env.get(provider.api_key_env))
            )
            if allowed(provider, task.privacy) and key_ok and not provider.paid:
                usable.append(ref)
        if usable:
            checks.append(
                Check(OK, f"Task '{name}' ({task.privacy.value})", "→ " + ", ".join(usable))
            )
        else:
            checks.append(
                Check(
                    WARN,
                    f"Task '{name}' ({task.privacy.value})",
                    "no usable model yet",
                    _task_fix(models, task.candidates, task.privacy),
                )
            )
    if (
        env.get("GROQ_API_KEY")
        and models.providers.get("groq")
        and models.providers["groq"].zero_data_retention
    ):
        checks.append(
            Check(
                WARN,
                "Groq zero data retention",
                "models.yaml assumes it is ON",
                "Confirm in console.groq.com → Settings → Data Controls → Zero Data Retention.",
            )
        )
    gemini = models.providers.get("gemini")
    if gemini is not None and allowed(gemini, PrivacyClass.PERSONAL):
        checks.append(
            Check(
                FAIL,
                "Gemini free tier",
                "is allowed to see personal data",
                "Set trains_on_data: true.",
            )
        )
    return checks


async def check_database(settings: Settings) -> Check:
    from jarvis.db.session import create_engine

    engine = create_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            vector = await conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        return Check(OK, "Database", f"migration {version}, pgvector {vector}")
    except Exception as exc:
        return Check(
            FAIL,
            "Database",
            f"{type(exc).__name__}: {str(exc)[:160]}",
            "Start it with `make up` and check DATABASE_URL / POSTGRES_PASSWORD in .env.",
        )
    finally:
        await engine.dispose()


async def check_ollama(
    settings: Settings, models: ModelsConfig | None, client: httpx.AsyncClient
) -> list[Check]:
    env = settings.provider_env()
    base = (
        (env.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1").rstrip("/").removesuffix("/v1")
    )
    checks: list[Check] = []
    vram = gpu_vram_gb()
    if vram is None:
        checks.append(
            Check(
                WARN,
                "GPU",
                "nvidia-smi not found",
                "Install NVIDIA drivers + the NVIDIA Container Toolkit (docs/setup.md).",
            )
        )
    else:
        checks.append(
            Check(
                OK,
                "GPU",
                f"{vram:.1f} GB VRAM → recommended local model {local_model_for_vram(vram)}",
            )
        )
    try:
        resp = await client.get(f"{base}/api/tags", timeout=5)
        pulled = {m["name"] for m in resp.json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        checks.append(Check(WARN, "Ollama", f"not reachable at {base}", "Start it with `make up`."))
        return checks
    wanted = [
        m.model
        for m in (models.models.values() if models else [])
        if models and models.providers[m.provider].kind == "ollama"
    ]
    missing = [w for w in wanted if w not in pulled and f"{w}:latest" not in pulled]
    if missing:
        checks.append(
            Check(
                WARN,
                "Ollama models",
                f"not pulled: {', '.join(missing)}",
                f"Run `make pull-models` (or `ollama pull {missing[0]}`).",
            )
        )
    else:
        checks.append(Check(OK, "Ollama", f"reachable, {len(pulled)} model(s) pulled"))
    return checks


async def check_voice(settings: Settings) -> list[Check]:
    """voice.yaml, the speech server and its models, and the sentence data."""
    try:
        config = load_voice_config(settings.voice_config_path)
    except VoiceConfigError as exc:
        fix = "Fix config/voice.yaml; voice stays off until then."
        return [Check(FAIL, "voice.yaml", str(exc), fix)]
    speech_config = config.speech
    checks = [
        Check(OK, "voice.yaml", f"voice {speech_config.voice}, hears {speech_config.language}")
    ]
    key = settings.speech_api_key.get_secret_value() if settings.speech_api_key else None
    if not key:
        checks.append(
            Check(WARN, "Speech server key", "SPEECH_API_KEY is empty", "Run `make secrets`.")
        )
    speech = SpeechClient(settings.speech_base_url, config.speech, api_key=key)
    try:
        status = await speech.status()
    finally:
        await speech.aclose()
    if status.key_refused:
        checks.append(
            Check(
                FAIL,
                "Speech server",
                status.detail,
                "Run `make up`, so Jarvis and the speech server both use the key in .env.",
            )
        )
    elif not status.reachable:
        checks.append(
            Check(
                WARN,
                "Speech server",
                f"not working at {settings.speech_base_url}: {status.detail}",
                "Start it with `make up`; if it stays down: `make logs SERVICE=speech`.",
            )
        )
    elif not (status.stt_ready and status.tts_ready):
        checks.append(
            Check(WARN, "Speech models", status.detail, "Download them with `make pull-models`.")
        )
    else:
        checks.append(Check(OK, "Speech server", "ready: hearing and speaking models downloaded"))
    if punkt_available():
        checks.append(Check(OK, "Voice sentence data", "installed"))
    else:
        checks.append(
            Check(
                WARN,
                "Voice sentence data",
                "missing (Jarvis tries to download it at startup)",
                "Run `make restart` with internet access: Jarvis downloads it as it starts.",
            )
        )
    return checks


async def check_telegram(settings: Settings, client: httpx.AsyncClient, *, online: bool) -> Check:
    token = (
        settings.telegram_bot_token.get_secret_value().strip()
        if settings.telegram_bot_token
        else ""
    )
    if not token:
        return Check(OK, "Telegram", "not set up (optional: see docs/setup.md)")
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
        return Check(
            FAIL,
            "Telegram",
            "TELEGRAM_BOT_TOKEN doesn't look like a bot token",
            "Copy it again from @BotFather (it looks like 123456789:AA...), then `make restart`.",
        )
    if not online:
        return Check(OK, "Telegram", "bot token set (ONLINE=1 tests it)")
    try:
        response = await client.post(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return Check(WARN, "Telegram", f"network error: {type(exc).__name__}")
    if not body.get("ok"):
        return Check(
            FAIL,
            "Telegram",
            "Telegram rejected the bot token",
            "Check TELEGRAM_BOT_TOKEN in .env (from @BotFather), then `make restart`.",
        )
    return Check(OK, "Telegram", f"bot @{body['result'].get('username')} is valid")


async def check_online(
    models: ModelsConfig, env: Mapping[str, str], client: httpx.AsyncClient
) -> list[Check]:
    """Verify API keys and that configured model IDs still exist."""
    checks: list[Check] = []
    endpoints = {
        "groq": (
            "https://api.groq.com/openai/v1/models",
            lambda k: {"Authorization": f"Bearer {k}"},
            None,
        ),
        "openrouter": (
            "https://openrouter.ai/api/v1/models",
            lambda k: {"Authorization": f"Bearer {k}"},
            None,
        ),
        "google": ("https://generativelanguage.googleapis.com/v1beta/models", lambda k: {}, "key"),
    }
    for name, provider in models.providers.items():
        if (
            provider.kind not in endpoints
            or not provider.api_key_env
            or not env.get(provider.api_key_env)
        ):
            continue
        url, headers, key_param = endpoints[provider.kind]
        key = env[provider.api_key_env]
        try:
            resp = await client.get(
                url,
                headers=headers(key),
                params={key_param: key} if key_param else None,
                timeout=15,
            )
        except httpx.HTTPError as exc:
            checks.append(Check(WARN, f"{name} API", f"network error: {type(exc).__name__}"))
            continue
        if resp.status_code in (401, 403):
            checks.append(
                Check(FAIL, f"{name} API key", "rejected", f"Check {provider.api_key_env} in .env.")
            )
            continue
        payload = resp.json()
        items = payload.get("data") or payload.get("models") or []
        ids = {str(i.get("id") or i.get("name", "")).removeprefix("models/") for i in items}
        for ref, model in models.models.items():
            if model.provider != name:
                continue
            if model.model in ids or model.model.endswith("-latest"):
                checks.append(Check(OK, f"{ref}", f"{model.model} available"))
            else:
                sample = ", ".join(sorted(ids)[:6])
                checks.append(
                    Check(
                        WARN,
                        f"{ref}",
                        f"{model.model} not listed by {name}",
                        f"Pick a current model in config/models.yaml, e.g. one of: {sample}",
                    )
                )
    return checks


# --- Email -------------------------------------------------------------------------------------

STALE_SYNC = timedelta(minutes=15)
SYNC_POINT_LIFE = timedelta(days=7)  # about how long Gmail keeps a place to sync from
_WHO = {"known": "people you know", "all": "everyone", "off": "nobody"}


def check_email_config(settings: Settings) -> Check:
    try:
        config = load_email_config(settings.email_config_path)
    except EmailConfigError as exc:
        return Check(FAIL, "email.yaml", str(exc), "Fix the file; Jarvis refuses to start with it.")
    return Check(
        OK,
        "email.yaml",
        f"auto-drafts for {_WHO[config.drafting.auto_draft]}, urgent alerts from "
        f"{_WHO[config.alerts.urgent]}, digests " + (" and ".join(config.digests.times) or "off"),
    )


def _ago(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 120:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    if minutes < 48 * 60:
        return f"{minutes // 60} hours"
    return f"{minutes // (24 * 60)} days"


def email_checks(connection: GoogleConnection, state: SyncState, now: datetime) -> list[Check]:
    """What Jarvis may do with your Gmail, and whether it's keeping up."""
    if not connection.connected:
        return [
            Check(
                WARN,
                "Gmail",
                "not connected",
                "In the app, open Sources and choose Connect Google (docs/setup.md, step 3).",
            )
        ]
    account = connection.account_email or "your account"
    checks: list[Check] = []
    if connection.mail_access == "full":
        access = f"{account}: read, label, draft and send (sending waits for you)"
        checks.append(Check(OK, "Gmail access", access))
    elif connection.mail_access == "read":
        checks.append(
            Check(
                WARN,
                "Gmail access",
                f"{account}: read only, so Jarvis sorts and summarises but can't label, "
                "draft or send",
                "In Sources, choose Give Jarvis your inbox and tick every box Google shows.",
            )
        )
    else:
        checks.append(
            Check(
                FAIL,
                "Gmail access",
                f"{account}: Google is connected, but Gmail wasn't allowed",
                "In Sources, connect Google again and tick the Gmail boxes.",
            )
        )
        return checks
    if connection.can_add_holds:
        checks.append(Check(OK, "Calendar holds", "allowed (private events, no invitees)"))
    else:
        checks.append(
            Check(
                WARN,
                "Calendar holds",
                "not allowed, so Add to calendar won't work",
                "In Sources, connect Google again and allow calendar events.",
            )
        )
    checks.append(_sync_check(state, now))
    return checks


def _sync_check(state: SyncState, now: datetime) -> Check:
    if state.status == "reconnect":
        return Check(
            FAIL,
            "Email sync",
            "Google stopped accepting Jarvis's access (changing your password does this)",
            "In Sources, connect Google again. Jarvis carries on from where it stopped.",
        )
    if state.last_sync_at is None:
        return Check(
            WARN,
            "Email sync",
            "Jarvis hasn't read your inbox yet",
            "Start Jarvis with `make up`; the first read takes a few minutes.",
        )
    ago = now - datetime.fromisoformat(state.last_sync_at)
    if state.status == "error":
        return Check(
            WARN,
            "Email sync",
            f"the last check failed ({state.error or 'no details'}); "
            f"the last good one was {_ago(ago)} ago",
            "Jarvis tries again every minute. If it keeps failing, see docs/runbook.md (Email).",
        )
    if ago > STALE_SYNC:
        later = (
            ". Gmail keeps Jarvis's place for about a week, so Jarvis will read your recent "
            "email again when it's back (automatically)"
            if ago > SYNC_POINT_LIFE
            else ""
        )
        return Check(
            WARN,
            "Email sync",
            f"last check {_ago(ago)} ago{later}",
            "Is Jarvis running? Start it with `make up`; `make logs` shows any email errors.",
        )
    return Check(OK, "Email sync", f"last check {_ago(ago)} ago")


async def check_email(
    settings: Settings, client: httpx.AsyncClient, *, online: bool, clock: Clock | None = None
) -> list[Check]:
    from jarvis.db.models import SystemState
    from jarvis.db.session import create_engine, create_session_factory, transaction
    from jarvis.ingestion.google import GoogleAuth, GoogleError
    from jarvis.mail.gmail import GmailAuthError, GmailClient, GmailError
    from jarvis.mail.sync import STATE_KEY
    from jarvis.security.crypto import Vault, VaultError

    checks = [check_email_config(settings)]
    if settings.secret_key is None:
        return checks  # the encryption key check says what to do
    try:
        vault = Vault(settings.secret_key.get_secret_value())
    except VaultError:
        return checks
    engine = create_engine(settings.database_url)
    try:
        factory = create_session_factory(engine)
        clock = clock or SystemClock()
        google = GoogleAuth.from_settings(settings, vault=vault, clock=clock, http=client)
        try:
            async with factory() as session:
                connection = await google.connection(session)
                row = await session.get(SystemState, STATE_KEY)
        except Exception as exc:  # the database check explains what's wrong
            reason = f"couldn't read Jarvis's email state ({type(exc).__name__})"
            return [*checks, Check(WARN, "Gmail", reason, "Fix the Database check first.")]
        state = SyncState.load(row.value if row is not None else None)
        checks += email_checks(connection, state, clock.now())
        if not online or connection.mail_access == "none":
            return checks

        async def token() -> str:
            async with transaction(factory) as session:
                return await google.access_token(session)

        gmail = GmailClient(client, token, endpoints=google.endpoints, backoff=0.0)
        try:
            profile = await gmail.profile()
            labels = {str(label.get("name")) for label in await gmail.labels()}
        except (GoogleError, GmailAuthError):
            fix = "In Sources, connect Google again."
            return [*checks, Check(FAIL, "Gmail (online)", "Google refused Jarvis's access", fix)]
        except (GmailError, httpx.HTTPError) as exc:
            reason = f"couldn't reach Gmail ({type(exc).__name__})"
            fix = "Check the PC's internet connection, then run `make doctor ONLINE=1` again."
            return [*checks, Check(WARN, "Gmail (online)", reason, fix)]
        address = profile.get("emailAddress") or connection.account_email
        checks.append(Check(OK, "Gmail (online)", f"reachable as {address}"))
        made = [name for name in JARVIS_LABELS.values() if name in labels]
        checks.append(
            Check(
                OK,
                "Jarvis labels",
                f"all {len(made)} in Gmail"
                if len(made) == len(JARVIS_LABELS)
                else f"{len(made)} of {len(JARVIS_LABELS)} in Gmail; Jarvis adds the rest "
                "the first time it uses them (once autonomy is on)",
            )
        )
        return checks
    finally:
        await engine.dispose()


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{_ICONS[c.status]} {c.title}: {c.detail}".rstrip(": "))
        if c.fix and c.status != OK:
            lines.append(f"    fix: {c.fix}")
    return "\n".join(lines)


async def run_doctor(*, online: bool = False) -> int:
    settings = get_settings()
    checks = check_settings(settings)
    config_checks, models = check_configs(settings)
    checks += config_checks
    checks.append(await check_database(settings))
    checks += await check_voice(settings)
    async with httpx.AsyncClient() as client:
        checks += await check_email(settings, client, online=online)
        checks.append(await check_telegram(settings, client, online=online))
        checks += await check_ollama(settings, models, client)
        if models is not None:
            checks += check_privacy(models, settings.provider_env())
            if online:
                checks += await check_online(models, settings.provider_env(), client)
    print(render(checks))
    failed = [c for c in checks if c.status == FAIL]
    warned = [c for c in checks if c.status == WARN]
    print(
        f"\n{len(checks) - len(failed) - len(warned)} ok, "
        f"{len(warned)} warning(s), {len(failed)} problem(s)."
    )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(run_doctor()))
