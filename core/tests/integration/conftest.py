"""Fixtures for the API tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from jarvis.api.app import create_app
from jarvis.clock import FrozenClock
from jarvis.db.session import SessionFactory
from jarvis.mail.config import EmailConfig
from jarvis.mail.service import MailService
from jarvis.mail.store import MailStore
from jarvis.mail.sync import MailAccess, MailSync
from jarvis.memory.embeddings import HashEmbedder
from jarvis.services import Services, build_services
from tests.conftest import fake_models_config, make_settings
from tests.fake_gmail import FakeGmail
from tests.integration.api_helpers import ORIGIN, Harness
from tests.integration.mail_helpers import OWNER, MailRig, connect_google
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


@pytest.fixture
async def mail(services: Services, clock: FrozenClock) -> AsyncIterator[MailRig]:
    """A fake Gmail, and Jarvis's mail sync wired to it."""
    fake = FakeGmail(account=OWNER, now=clock.now)
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        store = MailStore(services.vault, clock)
        rig = MailRig(
            services=services,
            clock=clock,
            fake=fake,
            http=http,
            store=store,
            sync=None,  # type: ignore[arg-type]
            access=MailAccess("full", OWNER),
        )

        async def access() -> MailAccess:
            return rig.access

        rig.sync = MailSync(
            session_factory=services.session_factory,
            clock=clock,
            store=store,
            config=EmailConfig(),
            client=rig.client,
            access=access,
        )
        yield rig


@pytest.fixture
async def mailer(mail: MailRig) -> MailService:
    """The whole mail service, with Google connected (inbox and calendar holds)."""
    google = await connect_google(mail)
    return MailService(mail.services, google=google, http=mail.http, config=EmailConfig())
