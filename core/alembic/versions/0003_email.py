"""Email (Phase 3): Gmail threads and messages, contacts, and Jarvis's reply drafts.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25 11:12:31.542771+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_contacts",
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("received_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("address", name=op.f("pk_mail_contacts")),
    )
    op.create_table(
        "mail_threads",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subject_enc", sa.LargeBinary(), nullable=True),
        sa.Column("participants", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_inbound_id", sa.String(length=64), nullable=True),
        sa.Column("in_inbox", sa.Boolean(), nullable=False),
        sa.Column("unread", sa.Boolean(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("needs_reply", sa.Boolean(), nullable=False),
        sa.Column("summary_enc", sa.LargeBinary(), nullable=True),
        sa.Column("details_enc", sa.LargeBinary(), nullable=True),
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("triaged_message_id", sa.String(length=64), nullable=True),
        sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triage_model", sa.String(length=64), nullable=True),
        sa.Column("owner_category", sa.String(length=16), nullable=True),
        sa.Column("owner_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_threads")),
    )
    op.create_index(op.f("ix_mail_threads_category"), "mail_threads", ["category"], unique=False)
    op.create_index(op.f("ix_mail_threads_in_inbox"), "mail_threads", ["in_inbox"], unique=False)
    op.create_index(
        op.f("ix_mail_threads_last_message_at"), "mail_threads", ["last_message_at"], unique=False
    )
    op.create_table(
        "mail_drafts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("reply_to_message_id", sa.String(length=64), nullable=True),
        sa.Column("proposal_id", sa.UUID(), nullable=True),
        sa.Column("content_enc", sa.LargeBinary(), nullable=True),
        sa.Column("original_hash", sa.String(length=64), nullable=True),
        sa.Column("gmail_draft_id", sa.String(length=64), nullable=True),
        sa.Column("gmail_body_hash", sa.String(length=64), nullable=True),
        sa.Column("origin", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("sent_message_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["action_proposals.id"],
            name=op.f("fk_mail_drafts_proposal_id_action_proposals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["mail_threads.id"],
            name=op.f("fk_mail_drafts_thread_id_mail_threads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_drafts")),
    )
    op.create_index(op.f("ix_mail_drafts_status"), "mail_drafts", ["status"], unique=False)
    op.create_index(op.f("ix_mail_drafts_thread_id"), "mail_drafts", ["thread_id"], unique=False)
    op.create_table(
        "mail_messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("history_id", sa.BigInteger(), nullable=False),
        sa.Column("internal_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("label_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("to_addresses", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cc_addresses", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reply_to", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("names_enc", sa.LargeBinary(), nullable=True),
        sa.Column("subject_enc", sa.LargeBinary(), nullable=True),
        sa.Column("snippet_enc", sa.LargeBinary(), nullable=True),
        sa.Column("body_enc", sa.LargeBinary(), nullable=True),
        sa.Column("attachments_enc", sa.LargeBinary(), nullable=True),
        sa.Column("has_attachments", sa.Boolean(), nullable=False),
        sa.Column("message_id_header", sa.Text(), nullable=True),
        sa.Column("references", sa.Text(), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["mail_threads.id"],
            name=op.f("fk_mail_messages_thread_id_mail_threads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_messages")),
    )
    op.create_index(
        op.f("ix_mail_messages_from_address"), "mail_messages", ["from_address"], unique=False
    )
    op.create_index(
        op.f("ix_mail_messages_internal_date"), "mail_messages", ["internal_date"], unique=False
    )
    op.create_index(
        op.f("ix_mail_messages_thread_id"), "mail_messages", ["thread_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_mail_messages_thread_id"), table_name="mail_messages")
    op.drop_index(op.f("ix_mail_messages_internal_date"), table_name="mail_messages")
    op.drop_index(op.f("ix_mail_messages_from_address"), table_name="mail_messages")
    op.drop_table("mail_messages")
    op.drop_index(op.f("ix_mail_drafts_thread_id"), table_name="mail_drafts")
    op.drop_index(op.f("ix_mail_drafts_status"), table_name="mail_drafts")
    op.drop_table("mail_drafts")
    op.drop_index(op.f("ix_mail_threads_last_message_at"), table_name="mail_threads")
    op.drop_index(op.f("ix_mail_threads_in_inbox"), table_name="mail_threads")
    op.drop_index(op.f("ix_mail_threads_category"), table_name="mail_threads")
    op.drop_table("mail_threads")
    op.drop_table("mail_contacts")
