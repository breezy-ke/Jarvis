"""Initial schema: foundations (Phase 0) and profile/memory (Phase 1).

Revision ID: 0001
Revises:
Create Date: 2026-09-24 16:54:23.604115+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "action_proposals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_canonical", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("autonomy", sa.String(length=4), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "validation", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_via", sa.String(length=24), nullable=True),
        sa.Column("approved_hash", sa.String(length=64), nullable=True),
        sa.Column("execute_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_proposals")),
    )
    op.create_index(
        op.f("ix_action_proposals_created_at"),
        "action_proposals",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_action_proposals_due",
        "action_proposals",
        ["status", "execute_after"],
        unique=False,
    )
    op.create_index(
        op.f("ix_action_proposals_kind"), "action_proposals", ["kind"], unique=False
    )
    op.create_index(
        op.f("ix_action_proposals_status"), "action_proposals", ["status"], unique=False
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=True),
        sa.Column("subject_id", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("canonical", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
        sa.UniqueConstraint("hash", name=op.f("uq_audit_events_hash")),
    )
    op.create_index(
        op.f("ix_audit_events_event_type"), "audit_events", ["event_type"], unique=False
    )
    op.create_index(op.f("ix_audit_events_ts"), "audit_events", ["ts"], unique=False)
    op.create_table(
        "auth_challenges",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_challenges")),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("step_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("memory_extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        op.f("ix_conversations_updated_at"),
        "conversations",
        ["updated_at"],
        unique=False,
    )
    op.create_table(
        "facts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("predicate", sa.String(length=200), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True
        ),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(subject, '') || ' ' || coalesce(predicate, '') || ' ' || coalesce(value, ''))",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_facts")),
    )
    op.create_index(op.f("ix_facts_category"), "facts", ["category"], unique=False)
    op.create_index(
        "ix_facts_current_subject_predicate",
        "facts",
        ["subject", "predicate"],
        unique=False,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index(
        "ix_facts_embedding",
        "facts",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_facts_search_tsv",
        "facts",
        ["search_tsv"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(op.f("ix_facts_status"), "facts", ["status"], unique=False)
    op.create_table(
        "ingestion_sources",
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("consent", sa.Boolean(), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("source", name=op.f("pk_ingestion_sources")),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task", sa.String(length=48), nullable=False),
        sa.Column("privacy", sa.String(length=16), nullable=False),
        sa.Column("model_ref", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index(
        "ix_llm_calls_model_ts", "llm_calls", ["model_ref", "ts"], unique=False
    )
    op.create_table(
        "oauth_pending",
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("code_verifier_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state", name=op.f("pk_oauth_pending")),
    )
    op.create_table(
        "oauth_tokens",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("account_email", sa.String(length=320), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider", name=op.f("pk_oauth_tokens")),
    )
    op.create_table(
        "onboarding_modules",
        sa.Column("module_id", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "transcript", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("module_id", name=op.f("pk_onboarding_modules")),
    )
    op.create_table(
        "profile_versions",
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=48), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("signed_off_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_profile_versions")),
    )
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_subscriptions")),
        sa.UniqueConstraint("endpoint", name=op.f("uq_push_subscriptions_endpoint")),
    )
    op.create_table(
        "recovery_codes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recovery_codes")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_recovery_codes_code_hash")),
    )
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_state")),
    )
    op.create_table(
        "webauthn_credentials",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "transports", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("device_name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webauthn_credentials")),
        sa.UniqueConstraint(
            "credential_id", name=op.f("uq_webauthn_credentials_credential_id")
        ),
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model_ref", sa.String(length=64), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_chat_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
    )
    op.create_index(
        op.f("ix_chat_messages_conversation_id"),
        "chat_messages",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_messages_created_at"),
        "chat_messages",
        ["created_at"],
        unique=False,
    )
    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "model_messages", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_turns_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_turns")),
    )
    op.create_index(
        op.f("ix_conversation_turns_conversation_id"),
        "conversation_turns",
        ["conversation_id"],
        unique=False,
    )
    op.create_table(
        "episodes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True
        ),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(summary, ''))", persisted=True
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_episodes_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episodes")),
    )
    op.create_index(
        op.f("ix_episodes_conversation_id"),
        "episodes",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_episodes_created_at"), "episodes", ["created_at"], unique=False
    )
    op.create_index(
        "ix_episodes_search_tsv",
        "episodes",
        ["search_tsv"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_episodes_search_tsv", table_name="episodes", postgresql_using="gin"
    )
    op.drop_index(op.f("ix_episodes_created_at"), table_name="episodes")
    op.drop_index(op.f("ix_episodes_conversation_id"), table_name="episodes")
    op.drop_table("episodes")
    op.drop_index(
        op.f("ix_conversation_turns_conversation_id"), table_name="conversation_turns"
    )
    op.drop_table("conversation_turns")
    op.drop_index(op.f("ix_chat_messages_created_at"), table_name="chat_messages")
    op.drop_index(op.f("ix_chat_messages_conversation_id"), table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_table("webauthn_credentials")
    op.drop_table("system_state")
    op.drop_table("recovery_codes")
    op.drop_table("push_subscriptions")
    op.drop_table("profile_versions")
    op.drop_table("onboarding_modules")
    op.drop_table("oauth_tokens")
    op.drop_table("oauth_pending")
    op.drop_index("ix_llm_calls_model_ts", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_table("ingestion_sources")
    op.drop_index(op.f("ix_facts_status"), table_name="facts")
    op.drop_index("ix_facts_search_tsv", table_name="facts", postgresql_using="gin")
    op.drop_index(
        "ix_facts_embedding",
        table_name="facts",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_index(
        "ix_facts_current_subject_predicate",
        table_name="facts",
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.drop_index(op.f("ix_facts_category"), table_name="facts")
    op.drop_table("facts")
    op.drop_index(op.f("ix_conversations_updated_at"), table_name="conversations")
    op.drop_table("conversations")
    op.drop_table("auth_sessions")
    op.drop_table("auth_challenges")
    op.drop_index(op.f("ix_audit_events_ts"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_event_type"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(op.f("ix_action_proposals_status"), table_name="action_proposals")
    op.drop_index(op.f("ix_action_proposals_kind"), table_name="action_proposals")
    op.drop_index("ix_action_proposals_due", table_name="action_proposals")
    op.drop_index(op.f("ix_action_proposals_created_at"), table_name="action_proposals")
    op.drop_table("action_proposals")
