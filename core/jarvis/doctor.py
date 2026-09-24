"""`jarvis doctor`: check the setup and say exactly how to fix what's missing."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from sqlalchemy import text

from jarvis.config import Settings, get_settings
from jarvis.llm.config import ModelsConfig, PrivacyClass, load_models_config
from jarvis.llm.privacy import allowed
from jarvis.policy.config import load_policies

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
    async with httpx.AsyncClient() as client:
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
