"""Single-owner authentication with passkeys (WebAuthn).

* **First run:** registering the first passkey needs a one-time setup token,
  printed in the server log and by `jarvis setup-token`. Nobody else on your
  network can claim Jarvis before you do.
* **Login:** a discoverable passkey with user verification (Face ID,
  fingerprint or PIN).
* **Step-up:** high-risk approvals need a fresh passkey tap (within 2
  minutes), consumed by one approval.
* **Recovery:** ten single-use recovery codes, shown once and stored hashed.
* **Sessions:** a random token in an HttpOnly, SameSite=Strict cookie. Only its
  SHA-256 is stored.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from jarvis.audit.log import AuditLog
from jarvis.clock import Clock
from jarvis.config import Settings
from jarvis.db.models import (
    AuthChallenge,
    AuthSession,
    RecoveryCode,
    SystemState,
    WebAuthnCredential,
)
from jarvis.security.hashing import sha256_hex

SESSION_COOKIE = "jarvis_session"
CHALLENGE_TTL = timedelta(minutes=5)
STEP_UP_TTL = timedelta(minutes=2)
SETUP_TOKEN_TTL = timedelta(hours=24)
SETUP_TOKEN_KEY = "setup_token"  # noqa: S105 (a storage key, not a secret)
OWNER_USER_ID = b"jarvis-owner"
RECOVERY_CODES = 10


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class NewSession:
    session: AuthSession
    token: str


def _recovery_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alikes (0/O, 1/I)
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _normalize_code(code: str) -> str:
    cleaned = "".join(c for c in code.upper() if c.isalnum())
    return f"{cleaned[:4]}-{cleaned[4:8]}-{cleaned[8:]}"


class AuthService:
    def __init__(self, *, settings: Settings, audit: AuditLog, clock: Clock) -> None:
        self._settings = settings
        self._audit = audit
        self._clock = clock
        self._failed_recovery: list[datetime] = []

    # --- Setup token ------------------------------------------------------------------

    async def issue_setup_token(self, session: AsyncSession) -> str:
        token = secrets.token_urlsafe(18)
        now = self._clock.now()
        row = await session.get(SystemState, SETUP_TOKEN_KEY, with_for_update=True)
        value = {"hash": sha256_hex(token), "expires_at": (now + SETUP_TOKEN_TTL).isoformat()}
        if row is None:
            session.add(SystemState(key=SETUP_TOKEN_KEY, value=value, updated_at=now))
        else:
            row.value, row.updated_at = value, now
        await self._audit.append(
            session, actor="system", event_type="auth.setup_token", summary="Issued a setup token"
        )
        return token

    async def _consume_setup_token(self, session: AsyncSession, token: str | None) -> bool:
        if not token:
            return False
        row = await session.get(SystemState, SETUP_TOKEN_KEY, with_for_update=True)
        if row is None or not row.value.get("hash"):
            return False
        if datetime.fromisoformat(row.value["expires_at"]) < self._clock.now():
            return False
        if not secrets.compare_digest(row.value["hash"], sha256_hex(token.strip())):
            return False
        row.value = {}
        return True

    async def has_credentials(self, session: AsyncSession) -> bool:
        count = await session.scalar(select(func.count()).select_from(WebAuthnCredential))
        return bool(count)

    # --- Challenges -------------------------------------------------------------------

    async def _new_challenge(
        self, session: AsyncSession, purpose: str, auth_session: AuthSession | None = None
    ) -> AuthChallenge:
        now = self._clock.now()
        challenge = AuthChallenge(
            id=uuid.uuid4(),
            purpose=purpose,
            challenge=os.urandom(32),
            session_id=auth_session.id if auth_session else None,
            created_at=now,
            expires_at=now + CHALLENGE_TTL,
        )
        session.add(challenge)
        await session.flush()
        return challenge

    async def _use_challenge(
        self, session: AsyncSession, challenge_id: str, purpose: str
    ) -> AuthChallenge:
        try:
            cid = uuid.UUID(challenge_id)
        except ValueError as exc:
            raise AuthError("Invalid challenge.") from exc
        challenge = await session.get(AuthChallenge, cid, with_for_update=True)
        if (
            challenge is None
            or challenge.purpose != purpose
            or challenge.used_at is not None
            or challenge.expires_at < self._clock.now()
        ):
            raise AuthError("This request expired. Please try again.")
        challenge.used_at = self._clock.now()
        return challenge

    # --- Registration -----------------------------------------------------------------

    async def registration_options(
        self, session: AsyncSession, *, auth_session: AuthSession | None, setup_token: str | None
    ) -> dict[str, Any]:
        existing = list(await session.scalars(select(WebAuthnCredential)))
        if existing and auth_session is None:
            raise AuthError("Jarvis is already set up. Log in with your passkey.", 403)
        if not existing:
            row = await session.get(SystemState, SETUP_TOKEN_KEY)
            valid = (
                row is not None
                and row.value.get("hash")
                and setup_token
                and secrets.compare_digest(row.value["hash"], sha256_hex(setup_token.strip()))
                and datetime.fromisoformat(row.value["expires_at"]) >= self._clock.now()
            )
            if not valid:
                raise AuthError(
                    "Enter the setup code from the Jarvis server log (or run `make setup-token`).",
                    403,
                )
        challenge = await self._new_challenge(session, "register", auth_session)
        options = generate_registration_options(
            rp_id=self._settings.rp_id,
            rp_name="Jarvis",
            user_name="owner",
            user_id=OWNER_USER_ID,
            user_display_name="Jarvis owner",
            challenge=challenge.challenge,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=c.credential_id) for c in existing
            ],
        )
        return {"challenge_id": str(challenge.id), "options": _json(options)}

    async def verify_registration(
        self,
        session: AsyncSession,
        *,
        challenge_id: str,
        credential: dict[str, Any],
        device_name: str,
        auth_session: AuthSession | None,
        setup_token: str | None,
        user_agent: str | None,
    ) -> tuple[NewSession | None, list[str] | None]:
        challenge = await self._use_challenge(session, challenge_id, "register")
        first = not await self.has_credentials(session)
        if first:
            if not await self._consume_setup_token(session, setup_token):
                raise AuthError("The setup code is missing, wrong or expired.", 403)
        elif auth_session is None or challenge.session_id != auth_session.id:
            raise AuthError("Log in before adding another passkey.", 403)
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge.challenge,
                expected_rp_id=self._settings.rp_id,
                expected_origin=self._settings.public_origin,
                require_user_verification=True,
            )
        except InvalidRegistrationResponse as exc:
            raise AuthError(f"Passkey registration failed: {exc}") from exc
        now = self._clock.now()
        session.add(
            WebAuthnCredential(
                id=uuid.uuid4(),
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
                transports=list(credential.get("response", {}).get("transports", []) or []),
                device_name=(device_name or "Passkey")[:100],
                created_at=now,
            )
        )
        await self._audit.append(
            session,
            actor="owner",
            event_type="auth.passkey_added",
            summary=f"Added passkey '{device_name or 'Passkey'}'",
        )
        codes = await self._replace_recovery_codes(session) if first else None
        new_session = await self.create_session(session, user_agent=user_agent) if first else None
        return new_session, codes

    # --- Login ------------------------------------------------------------------------

    async def login_options(self, session: AsyncSession) -> dict[str, Any]:
        challenge = await self._new_challenge(session, "login")
        options = generate_authentication_options(
            rp_id=self._settings.rp_id,
            challenge=challenge.challenge,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return {"challenge_id": str(challenge.id), "options": _json(options)}

    async def _verify_assertion(
        self, session: AsyncSession, challenge: AuthChallenge, credential: dict[str, Any]
    ) -> WebAuthnCredential:
        try:
            raw_id = base64url_to_bytes(str(credential.get("rawId") or credential.get("id")))
        except (ValueError, TypeError) as exc:
            raise AuthError("Unrecognised passkey.", 401) from exc
        stored = await session.scalar(
            select(WebAuthnCredential)
            .where(WebAuthnCredential.credential_id == raw_id)
            .with_for_update()
        )
        if stored is None:
            raise AuthError("Unrecognised passkey.", 401)
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge.challenge,
                expected_rp_id=self._settings.rp_id,
                expected_origin=self._settings.public_origin,
                credential_public_key=stored.public_key,
                credential_current_sign_count=stored.sign_count,
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse as exc:
            raise AuthError("Passkey check failed.", 401) from exc
        stored.sign_count = verified.new_sign_count
        stored.last_used_at = self._clock.now()
        return stored

    async def verify_login(
        self,
        session: AsyncSession,
        *,
        challenge_id: str,
        credential: dict[str, Any],
        user_agent: str | None,
    ) -> NewSession:
        challenge = await self._use_challenge(session, challenge_id, "login")
        stored = await self._verify_assertion(session, challenge, credential)
        await self._audit.append(
            session,
            actor="owner",
            event_type="auth.login",
            summary=f"Logged in with '{stored.device_name}'",
        )
        return await self.create_session(session, user_agent=user_agent)

    # --- Step-up (fresh passkey tap for high-risk approvals) --------------------------------

    async def step_up_options(
        self, session: AsyncSession, auth_session: AuthSession
    ) -> dict[str, Any]:
        challenge = await self._new_challenge(session, "step_up", auth_session)
        credentials = list(await session.scalars(select(WebAuthnCredential)))
        options = generate_authentication_options(
            rp_id=self._settings.rp_id,
            challenge=challenge.challenge,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=c.credential_id) for c in credentials
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return {"challenge_id": str(challenge.id), "options": _json(options)}

    async def verify_step_up(
        self,
        session: AsyncSession,
        *,
        auth_session: AuthSession,
        challenge_id: str,
        credential: dict[str, Any],
    ) -> None:
        challenge = await self._use_challenge(session, challenge_id, "step_up")
        if challenge.session_id != auth_session.id:
            raise AuthError("This confirmation belongs to another session.", 403)
        await self._verify_assertion(session, challenge, credential)
        live = await session.get(AuthSession, auth_session.id, with_for_update=True)
        assert live is not None
        live.step_up_at = self._clock.now()

    async def consume_step_up(self, session: AsyncSession, auth_session: AuthSession) -> bool:
        """True if a fresh step-up exists; it is consumed so each approval needs its own tap."""
        live = await session.get(AuthSession, auth_session.id, with_for_update=True)
        if live is None or live.step_up_at is None:
            return False
        fresh = self._clock.now() - live.step_up_at <= STEP_UP_TTL
        live.step_up_at = None
        return fresh

    # --- Recovery codes ------------------------------------------------------------------

    async def _replace_recovery_codes(self, session: AsyncSession) -> list[str]:
        for old in await session.scalars(select(RecoveryCode)):
            await session.delete(old)
        now = self._clock.now()
        codes = [_recovery_code() for _ in range(RECOVERY_CODES)]
        for code in codes:
            session.add(RecoveryCode(id=uuid.uuid4(), code_hash=sha256_hex(code), created_at=now))
        return codes

    async def regenerate_recovery_codes(
        self, session: AsyncSession, auth_session: AuthSession
    ) -> list[str]:
        if not await self.consume_step_up(session, auth_session):
            raise AuthError("Confirm with your passkey first.", 403)
        codes = await self._replace_recovery_codes(session)
        await self._audit.append(
            session,
            actor="owner",
            event_type="auth.recovery_codes",
            summary="Regenerated recovery codes",
        )
        return codes

    async def login_with_recovery_code(
        self, session: AsyncSession, code: str, *, user_agent: str | None
    ) -> NewSession:
        now = self._clock.now()
        self._failed_recovery = [
            t for t in self._failed_recovery if now - t < timedelta(minutes=15)
        ]
        if len(self._failed_recovery) >= 10:
            raise AuthError("Too many attempts. Wait 15 minutes.", 429)
        row = await session.scalar(
            select(RecoveryCode)
            .where(RecoveryCode.code_hash == sha256_hex(_normalize_code(code)))
            .where(RecoveryCode.used_at.is_(None))
            .with_for_update()
        )
        if row is None:
            self._failed_recovery.append(now)
            raise AuthError("That recovery code is not valid.", 401)
        row.used_at = now
        await self._audit.append(
            session,
            actor="owner",
            event_type="auth.recovery_login",
            summary="Logged in with a recovery code",
        )
        return await self.create_session(session, user_agent=user_agent)

    # --- Sessions ------------------------------------------------------------------------

    async def create_session(self, session: AsyncSession, *, user_agent: str | None) -> NewSession:
        token = secrets.token_urlsafe(32)
        now = self._clock.now()
        row = AuthSession(
            id=uuid.uuid4(),
            token_hash=sha256_hex(token),
            created_at=now,
            expires_at=now + timedelta(hours=self._settings.session_ttl_hours),
            last_seen_at=now,
            user_agent=(user_agent or "")[:300] or None,
        )
        session.add(row)
        await session.flush()
        return NewSession(row, token)

    async def resolve(self, session: AsyncSession, token: str | None) -> AuthSession | None:
        if not token:
            return None
        row = await session.scalar(
            select(AuthSession).where(AuthSession.token_hash == sha256_hex(token))
        )
        if row is None or row.revoked or row.expires_at < self._clock.now():
            return None
        return row

    async def logout(self, session: AsyncSession, auth_session: AuthSession) -> None:
        live = await session.get(AuthSession, auth_session.id, with_for_update=True)
        if live is not None:
            live.revoked = True

    async def credentials(self, session: AsyncSession) -> list[WebAuthnCredential]:
        return list(
            await session.scalars(
                select(WebAuthnCredential).order_by(WebAuthnCredential.created_at)
            )
        )

    async def remove_credential(
        self, session: AsyncSession, auth_session: AuthSession, credential_id: uuid.UUID
    ) -> None:
        if not await self.consume_step_up(session, auth_session):
            raise AuthError("Confirm with your passkey first.", 403)
        creds = await self.credentials(session)
        if len(creds) <= 1:
            raise AuthError("You can't remove your only passkey.")
        target = next((c for c in creds if c.id == credential_id), None)
        if target is None:
            raise AuthError("Passkey not found.", 404)
        await session.delete(target)
        await self._audit.append(
            session,
            actor="owner",
            event_type="auth.passkey_removed",
            summary=f"Removed passkey '{target.device_name}'",
        )


def _json(options: Any) -> dict[str, Any]:
    return json.loads(options_to_json(options))
