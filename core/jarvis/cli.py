"""Command line: `jarvis serve | migrate | setup-token | secrets | doctor | ...` (see --help)."""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import secrets
import sys
from pathlib import Path


def _serve(_: argparse.Namespace) -> int:
    import uvicorn

    from jarvis.api.app import create_app
    from jarvis.config import get_settings

    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level=settings.log_level.lower(),
    )
    return 0


def _migrate(_: argparse.Namespace) -> int:
    from jarvis.config import get_settings
    from jarvis.db.migrate import run_migrations
    from jarvis.db.session import create_engine

    async def go() -> None:
        engine = create_engine(get_settings().database_url)
        try:
            await run_migrations(engine)
        finally:
            await engine.dispose()

    asyncio.run(go())
    print("Database is up to date.")
    return 0


def _setup_token(_: argparse.Namespace) -> int:
    from jarvis.audit.log import AuditLog
    from jarvis.auth.service import AuthService
    from jarvis.clock import SystemClock
    from jarvis.config import get_settings
    from jarvis.db.session import create_engine, create_session_factory, transaction

    async def go() -> str:
        settings = get_settings()
        engine = create_engine(settings.database_url)
        try:
            clock = SystemClock()
            service = AuthService(settings=settings, audit=AuditLog(clock), clock=clock)
            async with transaction(create_session_factory(engine)) as session:
                return await service.issue_setup_token(session)
        finally:
            await engine.dispose()

    token = asyncio.run(go())
    print(f"Setup code (valid 24 hours): {token}")
    return 0


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_secrets() -> dict[str, str]:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return {
        "JARVIS_SECRET_KEY": Fernet.generate_key().decode(),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "VAPID_PRIVATE_KEY": _b64url(private_raw),
        "VAPID_PUBLIC_KEY": _b64url(public_raw),
        "SEARXNG_SECRET": secrets.token_hex(32),
        "SPEECH_API_KEY": secrets.token_urlsafe(32),
    }


def fill_env_file(path: Path, example: Path | None = None) -> list[str]:
    """Fill empty generated values in an .env file. Never overwrites a value you set."""
    if not path.exists():
        if example is None or not example.exists():
            raise FileNotFoundError(f"{path} does not exist and there is no .env.example to copy")
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    generated = generate_secrets()
    filled: list[str] = []
    out: list[str] = []
    present: set[str] = set()
    for line in lines:
        name, sep, value = line.partition("=")
        key = name.strip()
        if sep and not key.startswith("#"):
            present.add(key)
        if sep and key in generated and not value.split("#", 1)[0].strip():
            out.append(f"{key}={generated[key]}")
            filled.append(key)
        else:
            out.append(line)
    # A newer Jarvis may need a secret your older .env doesn't mention yet.
    missing = [key for key in generated if key not in present]
    if missing:
        out += ["", "# --- Added by `make secrets` for a newer version of Jarvis ---"]
        for key in missing:
            out.append(f"{key}={generated[key]}")
            filled.append(key)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return filled


def _secrets(args: argparse.Namespace) -> int:
    path = Path(args.env_file)
    filled = fill_env_file(path, path.parent / ".env.example")
    if filled:
        print(f"Generated {', '.join(filled)} in {path}.")
        print("Back up JARVIS_SECRET_KEY somewhere safe: without it, stored tokens can't be read.")
    else:
        print(f"{path} already has all generated secrets; nothing changed.")
    return 0


def local_model_ids(models_file: Path) -> list[str]:
    """Model IDs that run on the local Ollama server, in config order, without duplicates."""
    from jarvis.llm.config import load_models_config

    config = load_models_config(models_file)
    ids: list[str] = []
    for model in config.models.values():
        if config.providers[model.provider].kind == "ollama" and model.model not in ids:
            ids.append(model.model)
    return ids


def _local_models(_: argparse.Namespace) -> int:
    from jarvis.config import get_settings

    settings = get_settings()
    for model_id in local_model_ids(settings.models_file or settings.config_dir / "models.yaml"):
        print(model_id)
    return 0


def _fetch_text_data(args: argparse.Namespace) -> int:
    from jarvis.voice.textdata import fetch_punkt

    dest = Path(args.dest)
    if fetch_punkt(dest):
        print(f"Installed sentence-splitting data in {dest}.")
    else:
        print(f"Sentence-splitting data is already in {dest}.")
    return 0


def _pull_speech_models(_: argparse.Namespace) -> int:
    import httpx

    from jarvis.config import get_settings
    from jarvis.voice.config import VoiceConfigError, load_voice_config
    from jarvis.voice.speech import SpeechClient, SpeechError

    settings = get_settings()
    try:
        config = load_voice_config(settings.voice_config_path)
    except VoiceConfigError as exc:
        print(exc)
        return 1
    key = settings.speech_api_key.get_secret_value() if settings.speech_api_key else None

    async def pull() -> None:
        speech = SpeechClient(settings.speech_base_url, config.speech, api_key=key)
        try:
            for model in (config.speech.stt_model, config.speech.tts_model):
                print(f"Downloading {model} (the first time can take a few minutes)...", flush=True)
                await speech.pull(model)
                print("  ready", flush=True)
        finally:
            await speech.aclose()

    try:
        asyncio.run(pull())
    except SpeechError as exc:
        print(exc)
        return 1
    except httpx.HTTPError as exc:
        print(
            f"Can't reach the speech server at {settings.speech_base_url} "
            f"({type(exc).__name__}). Start it with `make up`."
        )
        return 1
    return 0


def _bench_voice(args: argparse.Namespace) -> int:
    from jarvis.clock import SystemClock
    from jarvis.config import get_settings
    from jarvis.db.session import create_engine, create_session_factory
    from jarvis.voice.bench import DEFAULT_AUDIO, BenchError, run_benchmark

    settings = get_settings()

    async def bench() -> bool:
        engine = create_engine(settings.database_url)
        try:
            report = await run_benchmark(
                create_session_factory(engine),
                clock=SystemClock(),
                url=args.url,
                runs=args.runs,
                question=Path(args.audio) if args.audio else DEFAULT_AUDIO,
            )
        finally:
            await engine.dispose()
        print(report.render())
        return report.passed

    print(f"Asking Jarvis a recorded question {args.runs} times over the voice socket...")
    try:
        return 0 if asyncio.run(bench()) else 1
    except BenchError as exc:
        print(exc)
        return 1


def _eval_triage(args: argparse.Namespace) -> int:
    from jarvis.config import get_settings
    from jarvis.mail.evaluate import run_triage_eval

    return asyncio.run(run_triage_eval(get_settings(), limit=args.limit))


def _doctor(args: argparse.Namespace) -> int:
    from jarvis.doctor import run_doctor

    return asyncio.run(run_doctor(online=args.online))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis personal assistant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the API and the PWA").set_defaults(func=_serve)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=_migrate)
    sub.add_parser("setup-token", help="print a first-run setup code").set_defaults(
        func=_setup_token
    )
    p_secrets = sub.add_parser("secrets", help="generate missing secrets in .env")
    p_secrets.add_argument("--env-file", default=".env")
    p_secrets.set_defaults(func=_secrets)
    p_doctor = sub.add_parser("doctor", help="check your setup and explain any fixes")
    p_doctor.add_argument(
        "--online", action="store_true", help="also test API keys over the network"
    )
    p_doctor.set_defaults(func=_doctor)
    sub.add_parser(
        "local-models", help="list the Ollama models config/models.yaml uses"
    ).set_defaults(func=_local_models)
    p_text = sub.add_parser(
        "fetch-text-data", help="download the voice pipeline's sentence-splitting data"
    )
    p_text.add_argument("--dest", default=os.environ.get("NLTK_DATA", "nltk_data"))
    p_text.set_defaults(func=_fetch_text_data)
    sub.add_parser(
        "pull-speech-models", help="download the speech models config/voice.yaml names"
    ).set_defaults(func=_pull_speech_models)
    p_bench = sub.add_parser("bench-voice", help="measure how fast Jarvis answers out loud")
    p_bench.add_argument("--runs", type=int, default=5)
    p_bench.add_argument("--url", default="ws://127.0.0.1:8080/api/voice/ws")
    p_bench.add_argument("--audio", help="a 16 kHz mono WAV question (default: a recorded one)")
    p_bench.set_defaults(func=_bench_voice)
    p_eval = sub.add_parser("eval", help="score how well Jarvis does its work on your own data")
    evals = p_eval.add_subparsers(dest="suite", required=True)
    p_triage = evals.add_parser(
        "triage", help="sorting email, against the emails you checked in the Inbox"
    )
    p_triage.add_argument(
        "--limit", type=int, default=200, help="how many checked emails, newest first"
    )
    p_triage.set_defaults(func=_eval_triage)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
