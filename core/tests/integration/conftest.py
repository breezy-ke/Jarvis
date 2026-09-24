"""Fixtures for the API tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from jarvis.api.app import create_app
from jarvis.clock import FrozenClock
from jarvis.db.session import SessionFactory
from jarvis.memory.embeddings import HashEmbedder
from jarvis.services import Services, build_services
from tests.conftest import fake_models_config, make_settings
from tests.integration.api_helpers import ORIGIN, Harness
from tests.webauthn_helpers import SoftAuthenticator


@pytest.fixture
async def harness(session_factory: SessionFactory, clock: FrozenClock) -> AsyncIterator[Harness]:
    settings = make_settings(JARVIS_ENABLE_SCHEDULER=False)

    def factory(s: Any, sf: SessionFactory) -> Services:
        return build_services(
            s, sf, clock=clock, embedder=HashEmbedder(), models_config=fake_models_config()
        )

    app = create_app(settings, services_factory=factory, run_background=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            yield Harness(
                app=app,
                client=client,
                state=app.state.jarvis,
                authenticator=SoftAuthenticator(rp_id="localhost", origin=ORIGIN),
                clock=clock,
            )
