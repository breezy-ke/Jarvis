"""Voice (Phase 2): paired voice satellites and Telegram approval messages.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24 19:10:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voice_devices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_devices")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_voice_devices_token_hash")),
    )
    op.create_table(
        "telegram_notices",
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("shown_status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["action_proposals.id"],
            name=op.f("fk_telegram_notices_proposal_id_action_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("proposal_id", name=op.f("pk_telegram_notices")),
    )


def downgrade() -> None:
    op.drop_table("telegram_notices")
    op.drop_table("voice_devices")
