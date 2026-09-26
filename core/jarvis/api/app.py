"""The FastAPI application: wiring, security headers, background workers, PWA."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from jarvis.api import actions, auth, chat, onboarding, profile, system
from jarvis.api import mail as mail_api
from jarvis.api import telegram as telegram_api
from jarvis.api import voice as voice_api
from jarvis.api.deps import AppState
from jarvis.auth.service import AuthService
from jarvis.chat.service import ChatService
from jarvis.config import Settings, get_settings
from jarvis.db.migrate import run_migrations
from jarvis.db.session import SessionFactory, create_engine, create_session_factory, transaction
from jarvis.ingestion.google import GoogleAuth
from jarvis.ingestion.service import IngestionService
from jarvis.mail.config import load_email_config
from jarvis.mail.service import MailService
from jarvis.onboarding.service import OnboardingService
from jarvis.services import Services, build_services
from jarvis.telegram.bot import build_telegram_bot
from jarvis.tracing import configure_tracing
from jarvis.voice.runtime import build_voice_runtime
from jarvis.workflows.scheduler import Scheduler, executor_loop

log = logging.getLogger("jarvis")

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Called by non-browser clients and authorised by a one-time code, never a
# cookie, so cross-site request forgery doesn't apply.
_NO_COOKIE_ENDPOINTS = frozenset({"/api/voice/devices/pair"})


def content_security_policy(public_origin: str) -> str:
    """The page's CSP. Jarvis's own WebSocket address is named explicitly for the
    voice socket, because older Safari doesn't count ws:/wss: as 'self'."""
    parts = urlsplit(public_origin)
    socket = f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}"
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
        f"connect-src 'self' {socket}; worker-src 'self'; manifest-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )


ServicesFactory = Callable[[Settings, SessionFactory], Services]


class SPAStaticFiles(StaticFiles):
    """Serve the built PWA, falling back to index.html for client-side routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith("api/"):
                raise
            response = await super().get_response("index.html", scope)
        if path in ("sw.js", "index.html", "") or path.endswith(".webmanifest"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def _banner(token: str) -> None:
    line = "=" * 64
    log.warning(
        "\n%s\n  JARVIS FIRST-RUN SETUP CODE: %s\n"
        "  Open Jarvis and enter this code to register your passkey.\n"
        "  (Valid 24h. Get a new one with `make setup-token`.)\n%s",
        line,
        token,
        line,
    )


def create_app(
    settings: Settings | None = None,
    *,
    services_factory: ServicesFactory | None = None,
    run_background: bool = True,
    http_transport: httpx.AsyncBaseTransport | None = None,  # tests: a fake Google
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        email_config = load_email_config(settings.email_config_path)  # invalid: say why and stop
        configure_tracing(settings)
        engine = create_engine(settings.database_url)
        if settings.auto_migrate:
            await run_migrations(engine)
        session_factory = create_session_factory(engine)
        factory = services_factory or build_services
        services = factory(settings, session_factory)
        http = httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": "Jarvis/0.1"}, transport=http_transport
        )
        google = GoogleAuth.from_settings(
            settings, vault=services.vault, clock=services.clock, http=http
        )
        auth_service = AuthService(settings=settings, audit=services.audit, clock=services.clock)
        voice = build_voice_runtime(settings, services)
        mail = MailService(services, google=google, http=http, config=email_config)
        chat_service = ChatService(services, mail=mail)
        telegram = build_telegram_bot(settings, services, chat=chat_service, voice=voice)
        mail.notifier.telegram = telegram
        app.state.jarvis = AppState(
            services=services,
            auth=auth_service,
            chat=chat_service,
            onboarding=OnboardingService(services),
            ingestion=IngestionService(services, http=http, google_auth=google),
            google=google,
            http=http,
            voice=voice,
            telegram=telegram,
            mail=mail,
        )

        async with transaction(session_factory) as session:
            interrupted = await services.policy.recover_interrupted(session)
            if interrupted:
                log.warning("%d action(s) were interrupted by a restart; review them", interrupted)
            if not await auth_service.has_credentials(session):
                _banner(await auth_service.issue_setup_token(session))

        stop = asyncio.Event()
        background: list[asyncio.Task[None]] = []
        scheduler = Scheduler(services, mail=mail)
        preparing = asyncio.create_task(voice.prepare())
        if run_background:
            background.append(asyncio.create_task(executor_loop(services, stop)))
            background.append(asyncio.create_task(mail.run(stop)))
            if telegram is not None:
                background.append(asyncio.create_task(telegram.run(stop)))
            if settings.enable_scheduler:
                try:
                    await scheduler.start()
                except Exception:  # Jarvis still works; periodic jobs are paused
                    log.exception("scheduler failed to start; periodic jobs are paused")
        try:
            yield
        finally:
            stop.set()
            preparing.cancel()  # a slow download mustn't hold up shutdown
            for task in [preparing, *background]:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await scheduler.stop()
            if telegram is not None:
                await telegram.aclose()
            await voice.aclose()
            await http.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Jarvis",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/api/openapi.json",
    )

    csp = content_security_policy(settings.public_origin)

    @app.middleware("http")
    async def security(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if (
            request.method in _UNSAFE_METHODS
            and request.url.path.startswith("/api/")
            and request.url.path not in _NO_COOKIE_ENDPOINTS
        ):
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            same_origin = origin == settings.public_origin or (
                origin is None and fetch_site == "same-origin"
            )
            if not same_origin:
                return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "microphone=(self), camera=(), geolocation=()"
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if settings.secure_cookies:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    for module in (
        auth,
        chat,
        actions,
        system,
        profile,
        onboarding,
        voice_api,
        telegram_api,
        mail_api,
    ):
        app.include_router(module.router)

    dist = settings.web_dist_dir
    if dist is not None and Path(dist).is_dir():
        app.mount("/", SPAStaticFiles(directory=str(dist), html=True), name="pwa")

    return app
