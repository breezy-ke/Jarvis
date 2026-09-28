"""Daily tech brief (Phase 4): sources, the stories they publish, briefs and your votes.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28 08:11:47.336119+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brief_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_items")),
        sa.UniqueConstraint("key", name=op.f("uq_brief_items_key")),
    )
    op.create_index(
        op.f("ix_brief_items_published_at"), "brief_items", ["published_at"], unique=False
    )
    op.create_index(op.f("ix_brief_items_source_id"), "brief_items", ["source_id"], unique=False)
    op.create_table(
        "brief_sources",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("items_seen", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_sources")),
    )
    op.create_table(
        "briefs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra_enc", sa.LargeBinary(), nullable=True),
        sa.Column("audio_file", sa.Text(), nullable=True),
        sa.Column("audio_seconds", sa.Integer(), nullable=True),
        sa.Column("models", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("deliveries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("on_time", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_briefs")),
        sa.UniqueConstraint("day", name=op.f("uq_briefs_day")),
    )
    op.create_index(op.f("ix_briefs_status"), "briefs", ["status"], unique=False)
    op.create_table(
        "brief_entries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("brief_id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=True),
        sa.Column("section", sa.String(length=16), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("note_enc", sa.LargeBinary(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("why", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True),
        sa.Column("vote", sa.Integer(), nullable=True),
        sa.Column("voted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["brief_id"],
            ["briefs.id"],
            name=op.f("fk_brief_entries_brief_id_briefs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["brief_items.id"],
            name=op.f("fk_brief_entries_item_id_brief_items"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_entries")),
    )
    op.create_index(op.f("ix_brief_entries_brief_id"), "brief_entries", ["brief_id"], unique=False)
    op.create_index(
        op.f("ix_brief_entries_source_id"), "brief_entries", ["source_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_brief_entries_source_id"), table_name="brief_entries")
    op.drop_index(op.f("ix_brief_entries_brief_id"), table_name="brief_entries")
    op.drop_table("brief_entries")
    op.drop_index(op.f("ix_briefs_status"), table_name="briefs")
    op.drop_table("briefs")
    op.drop_table("brief_sources")
    op.drop_index(op.f("ix_brief_items_source_id"), table_name="brief_items")
    op.drop_index(op.f("ix_brief_items_published_at"), table_name="brief_items")
    op.drop_table("brief_items")
