"""Cleaned copies of a transcript, proposed by the language model."""

import sqlalchemy as sa

from alembic import op

revision = "0011_transcript_cleanup"
down_revision = "0010_recording_speakers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transcript_cleanup",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_step", sa.String(length=50), nullable=True),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_transcript_cleanup_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["meeting_recording.id"],
            name=op.f("fk_transcript_cleanup_recording_id_meeting_recording"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcript_cleanup")),
    )
    op.create_index(
        op.f("ix_transcript_cleanup_recording_id"),
        "transcript_cleanup",
        ["recording_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_transcript_cleanup_recording_id"), table_name="transcript_cleanup"
    )
    op.drop_table("transcript_cleanup")
