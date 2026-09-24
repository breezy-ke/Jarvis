"""Shared helpers for API tests: an app harness and passkey sign-in steps."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import FastAPI

from jarvis.api.deps import AppState
from jarvis.clock import FrozenClock
from jarvis.db.session import transaction
from jarvis.services import Services
from tests.webauthn_helpers import SoftAuthenticator

ORIGIN = "http://localhost:8080"


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    state: AppState
    authenticator: SoftAuthenticator
    clock: FrozenClock

    @property
    def services(self) -> Services:
        return self.state.services


async def setup_token(h: Harness) -> str:
    async with transaction(h.services.session_factory) as session:
        return await h.state.auth.issue_setup_token(session)


async def register(h: Harness) -> list[str]:
    token = await setup_token(h)
    options = (
        await h.client.post("/api/auth/register/options", json={"setup_token": token})
    ).json()
    credential = h.authenticator.register(options["options"])
    resp = await h.client.post(
        "/api/auth/register/verify",
        json={
            "challenge_id": options["challenge_id"],
            "credential": credential,
            "device_name": "Test phone",
            "setup_token": token,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["recovery_codes"]


async def login(h: Harness) -> None:
    options = (await h.client.post("/api/auth/login/options")).json()
    resp = await h.client.post(
        "/api/auth/login/verify",
        json={
            "challenge_id": options["challenge_id"],
            "credential": h.authenticator.assertion(options["options"]),
        },
    )
    assert resp.status_code == 200, resp.text


async def step_up(h: Harness) -> None:
    options = (await h.client.post("/api/auth/step-up/options")).json()
    resp = await h.client.post(
        "/api/auth/step-up/verify",
        json={
            "challenge_id": options["challenge_id"],
            "credential": h.authenticator.assertion(options["options"]),
        },
    )
    assert resp.status_code == 200, resp.text
