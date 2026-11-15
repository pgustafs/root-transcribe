"""Speaker names typed by people, and runs that look for names in a transcript."""

import sqlalchemy as sa

from alembic import op

revision = "0004_speaker_names"
down_revision = "0003_meeting_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_transcript_segment",
        sa.Column("speaker_display_name", sa.String(length=120), nullable=True),
    )
    op.create_table(
        "speaker_name_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("suggestions", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_speaker_name_run_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["meeting_recording.id"],
            name=op.f("fk_speaker_name_run_recording_id_meeting_recording"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_speaker_name_run")),
    )
    op.create_index(
        op.f("ix_speaker_name_run_recording_id"),
        "speaker_name_run",
        ["recording_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_speaker_name_run_recording_id"), table_name="speaker_name_run"
    )
    op.drop_table("speaker_name_run")
    op.drop_column("meeting_transcript_segment", "speaker_display_name")
