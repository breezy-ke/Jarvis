"""Passkey login, first-run setup, step-up confirmation and recovery."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jarvis.api.deps import Owner, State
from jarvis.auth.service import SESSION_COOKIE, AuthError, NewSession
from jarvis.db.session import transaction

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterOptionsIn(BaseModel):
    setup_token: str | None = None


class RegisterVerifyIn(BaseModel):
    challenge_id: str
    credential: dict[str, Any]
    device_name: str = Field(default="Passkey", max_length=100)
    setup_token: str | None = None


class AssertionIn(BaseModel):
    challenge_id: str
    credential: dict[str, Any]


class RecoveryIn(BaseModel):
    code: str = Field(min_length=8, max_length=40)


def _set_cookie(response: Response, state: State, new: NewSession) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        new.token,
        max_age=state.services.settings.session_ttl_hours * 3600,
        httponly=True,
        secure=state.services.settings.secure_cookies,
        samesite="strict",
        path="/",
    )


def _fail(exc: AuthError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


async def _optional_session(request: Request, state: State) -> Any:
    async with state.services.session_factory() as session:
        return await state.auth.resolve(session, request.cookies.get(SESSION_COOKIE))


@router.get("/status")
async def status(request: Request, state: State) -> dict[str, Any]:
    async with state.services.session_factory() as session:
        has_credentials = await state.auth.has_credentials(session)
        current = await state.auth.resolve(session, request.cookies.get(SESSION_COOKIE))
    return {"setup_complete": has_credentials, "authenticated": current is not None}


@router.post("/register/options")
async def register_options(
    body: RegisterOptionsIn, request: Request, state: State
) -> dict[str, Any]:
    current = await _optional_session(request, state)
    try:
        async with transaction(state.services.session_factory) as session:
            return await state.auth.registration_options(
                session, auth_session=current, setup_token=body.setup_token
            )
    except AuthError as exc:
        raise _fail(exc) from exc


@router.post("/register/verify")
async def register_verify(
    body: RegisterVerifyIn, request: Request, response: Response, state: State
) -> dict[str, Any]:
    current = await _optional_session(request, state)
    try:
        async with transaction(state.services.session_factory) as session:
            new, codes = await state.auth.verify_registration(
                session,
                challenge_id=body.challenge_id,
                credential=body.credential,
                device_name=body.device_name,
                auth_session=current,
                setup_token=body.setup_token,
                user_agent=request.headers.get("user-agent"),
            )
    except AuthError as exc:
        raise _fail(exc) from exc
    if new is not None:
        _set_cookie(response, state, new)
    return {"ok": True, "recovery_codes": codes}


@router.post("/login/options")
async def login_options(state: State) -> dict[str, Any]:
    async with transaction(state.services.session_factory) as session:
        return await state.auth.login_options(session)


@router.post("/login/verify")
async def login_verify(
    body: AssertionIn, request: Request, response: Response, state: State
) -> dict[str, bool]:
    try:
        async with transaction(state.services.session_factory) as session:
            new = await state.auth.verify_login(
                session,
                challenge_id=body.challenge_id,
                credential=body.credential,
                user_agent=request.headers.get("user-agent"),
            )
    except AuthError as exc:
        raise _fail(exc) from exc
    _set_cookie(response, state, new)
    return {"ok": True}


@router.post("/recovery")
async def recovery(
    body: RecoveryIn, request: Request, response: Response, state: State
) -> dict[str, bool]:
    try:
        async with transaction(state.services.session_factory) as session:
            new = await state.auth.login_with_recovery_code(
                session, body.code, user_agent=request.headers.get("user-agent")
            )
    except AuthError as exc:
        raise _fail(exc) from exc
    _set_cookie(response, state, new)
    return {"ok": True}


@router.post("/logout")
async def logout(owner: Owner, response: Response, state: State) -> dict[str, bool]:
    async with transaction(state.services.session_factory) as session:
        await state.auth.logout(session, owner)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/step-up/options")
async def step_up_options(owner: Owner, state: State) -> dict[str, Any]:
    async with transaction(state.services.session_factory) as session:
        return await state.auth.step_up_options(session, owner)


@router.post("/step-up/verify")
async def step_up_verify(body: AssertionIn, owner: Owner, state: State) -> dict[str, bool]:
    try:
        async with transaction(state.services.session_factory) as session:
            await state.auth.verify_step_up(
                session,
                auth_session=owner,
                challenge_id=body.challenge_id,
                credential=body.credential,
            )
    except AuthError as exc:
        raise _fail(exc) from exc
    return {"ok": True}


@router.get("/passkeys")
async def passkeys(owner: Owner, state: State) -> list[dict[str, Any]]:
    async with state.services.session_factory() as session:
        creds = await state.auth.credentials(session)
    return [
        {
            "id": str(c.id),
            "device_name": c.device_name,
            "created_at": c.created_at.isoformat(),
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        }
        for c in creds
    ]


@router.delete("/passkeys/{credential_id}")
async def remove_passkey(credential_id: uuid.UUID, owner: Owner, state: State) -> dict[str, bool]:
    try:
        async with transaction(state.services.session_factory) as session:
            await state.auth.remove_credential(session, owner, credential_id)
    except AuthError as exc:
        raise _fail(exc) from exc
    return {"ok": True}


@router.post("/recovery-codes")
async def regenerate_codes(owner: Owner, state: State) -> dict[str, list[str]]:
    try:
        async with transaction(state.services.session_factory) as session:
            codes = await state.auth.regenerate_recovery_codes(session, owner)
    except AuthError as exc:
        raise _fail(exc) from exc
    return {"recovery_codes": codes}
