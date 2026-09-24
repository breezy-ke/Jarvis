"""Shared API dependencies: application state and the authenticated session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request

from jarvis.auth.service import SESSION_COOKIE, AuthService
from jarvis.chat.service import ChatService
from jarvis.db.models import AuthSession
from jarvis.ingestion.google import GoogleAuth
from jarvis.ingestion.service import IngestionService
from jarvis.onboarding.service import OnboardingService
from jarvis.services import Services


@dataclass
class AppState:
    services: Services
    auth: AuthService
    chat: ChatService
    onboarding: OnboardingService
    ingestion: IngestionService
    google: GoogleAuth
    http: httpx.AsyncClient


def get_state(request: Request) -> AppState:
    return request.app.state.jarvis


State = Annotated[AppState, Depends(get_state)]


async def require_session(request: Request, state: State) -> AuthSession:
    token = request.cookies.get(SESSION_COOKIE)
    async with state.services.session_factory() as session:
        auth_session = await state.auth.resolve(session, token)
    if auth_session is None:
        raise HTTPException(status_code=401, detail="Please log in.")
    return auth_session


Owner = Annotated[AuthSession, Depends(require_session)]
