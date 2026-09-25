"""ORM models.

Timestamps are set from the application clock rather than `now()` in SQL, so
time-based rules (undo windows, quotas, expiry) are fully testable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import EMBEDDING_DIM, Base

TZ = DateTime(timezone=True)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# --- System ------------------------------------------------------------------


class SystemState(Base):
    """Small key/value store for switches like the kill switch."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TZ)


class AuditEvent(Base):
    """Append-only, hash-chained record of everything Jarvis does.

    `canonical` is the exact JSON text that was hashed, so verification never
    depends on how the database round-trips timestamps or numbers. Keep `data`
    free of personal content (IDs and metadata only), so forgetting something
    never means rewriting the chain.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZ, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(128))
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    canonical: Mapped[str] = mapped_column(Text)


class ActionProposal(Base):
    """Something an agent wants to do in the outside world.

    Nothing executes unless `status == 'approved'` and `approved_hash` equals
    the SHA-256 of `payload_canonical`, the exact bytes that were approved.
    """

    __tablename__ = "action_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_canonical: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))
    risk: Mapped[str] = mapped_column(String(16))
    autonomy: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(24), index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    validation: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_by: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(TZ, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(TZ)
    decided_via: Mapped[str | None] = mapped_column(String(24))
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    execute_after: Mapped[datetime | None] = mapped_column(TZ)
    executed_at: Mapped[datetime | None] = mapped_column(TZ)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_action_proposals_due", "status", "execute_after"),)


# --- Conversations --------------------------------------------------------------


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    channel: Mapped[str] = mapped_column(String(24), default="pwa")
    created_at: Mapped[datetime] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ, index=True)
    memory_extracted_at: Mapped[datetime | None] = mapped_column(TZ)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


# The channel of the conversation `jarvis bench-voice` asks its questions in: a
# measurement, so it's never mined for memories.
BENCHMARK_CHANNEL = "benchmark"


class ChatMessage(Base):
    """A display-level message (what the owner sees in the chat UI)."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, index=True)
    model_ref: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ConversationTurn(Base):
    """Full model-level messages for a turn (tool calls included), for replay."""

    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZ)
    model_messages: Mapped[list[Any]] = mapped_column(JSONB)


# --- Model usage ---------------------------------------------------------------


class LLMCall(Base):
    """One model call. Used for quota tracking, budgets and the Activity page."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZ)
    task: Mapped[str] = mapped_column(String(48))
    privacy: Mapped[str] = mapped_column(String(16))
    model_ref: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=1)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (Index("ix_llm_calls_model_ts", "model_ref", "ts"),)


# --- Authentication ------------------------------------------------------------


class WebAuthnCredential(Base):
    __tablename__ = "webauthn_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0)
    transports: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    device_name: Mapped[str] = mapped_column(String(100), default="Passkey")
    created_at: Mapped[datetime] = mapped_column(TZ)
    last_used_at: Mapped[datetime | None] = mapped_column(TZ)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    last_seen_at: Mapped[datetime] = mapped_column(TZ)
    step_up_at: Mapped[datetime | None] = mapped_column(TZ)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class AuthChallenge(Base):
    __tablename__ = "auth_challenges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    purpose: Mapped[str] = mapped_column(String(16))
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(TZ)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    used_at: Mapped[datetime | None] = mapped_column(TZ)


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ)
    used_at: Mapped[datetime | None] = mapped_column(TZ)


class VoiceDevice(Base):
    """A paired voice satellite (e.g. the Windows tray app). Voice access only."""

    __tablename__ = "voice_devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)


class TelegramNotice(Base):
    """An approval request sent to the owner on Telegram, kept in step with its action."""

    __tablename__ = "telegram_notices"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_proposals.id", ondelete="CASCADE"),
        primary_key=True,
    )
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    shown_status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    keys: Mapped[dict[str, Any]] = mapped_column(JSONB)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(TZ)


# --- Profile and memory (Phase 1) -------------------------------------------------


class ProfileVersion(Base):
    """Every change to the owner's profile creates a new version."""

    __tablename__ = "profile_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ)
    created_by: Mapped[str] = mapped_column(String(48))
    note: Mapped[str] = mapped_column(Text, default="")
    signed_off_at: Mapped[datetime | None] = mapped_column(TZ)


_FACT_TSV = (
    "to_tsvector('english', coalesce(subject, '') || ' ' || coalesce(predicate, '') "
    "|| ' ' || coalesce(value, ''))"
)


class Fact(Base):
    """One remembered fact, with provenance and a validity window.

    New information supersedes old facts (sets `valid_to`) instead of
    overwriting them, so the history of what Jarvis believed is kept.
    """

    __tablename__ = "facts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    category: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(200))
    predicate: Mapped[str] = mapped_column(String(200))
    value: Mapped[str] = mapped_column(Text)
    sensitivity: Mapped[str] = mapped_column(String(16), default="normal")
    status: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(64))
    source_ref: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    valid_from: Mapped[datetime] = mapped_column(TZ)
    valid_to: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    search_tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed(_FACT_TSV, persisted=True))

    __table_args__ = (
        Index("ix_facts_search_tsv", "search_tsv", postgresql_using="gin"),
        Index(
            "ix_facts_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_facts_current_subject_predicate",
            "subject",
            "predicate",
            postgresql_where=text("valid_to IS NULL"),
        ),
    )


class Episode(Base):
    """A summary of a past conversation, for long-term recall."""

    __tablename__ = "episodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, index=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(summary, ''))", persisted=True)
    )

    __table_args__ = (Index("ix_episodes_search_tsv", "search_tsv", postgresql_using="gin"),)


class OnboardingModuleState(Base):
    __tablename__ = "onboarding_modules"

    module_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="not_started")
    transcript: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)


class IngestionSource(Base):
    __tablename__ = "ingestion_sources"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    consented_at: Mapped[datetime | None] = mapped_column(TZ)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(TZ)
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class OAuthToken(Base):
    """Encrypted OAuth tokens (for example Google). Never logged, never exported."""

    __tablename__ = "oauth_tokens"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    token_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    scopes: Mapped[str] = mapped_column(Text)
    account_email: Mapped[str | None] = mapped_column(String(320))
    updated_at: Mapped[datetime] = mapped_column(TZ)


class OAuthPending(Base):
    """An OAuth flow in progress: its state value and encrypted PKCE verifier."""

    __tablename__ = "oauth_pending"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    code_verifier_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    redirect_uri: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ)
    expires_at: Mapped[datetime] = mapped_column(TZ)
