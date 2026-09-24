"""Command line: `jarvis serve | migrate | setup-token | secrets | doctor | local-models`."""

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
    for line in lines:
        name, sep, value = line.partition("=")
        key = name.strip()
        if sep and key in generated and not value.split("#", 1)[0].strip():
            out.append(f"{key}={generated[key]}")
            filled.append(key)
        else:
            out.append(line)
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
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
