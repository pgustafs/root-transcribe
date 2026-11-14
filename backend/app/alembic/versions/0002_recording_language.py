"""Persist the requested recording language separately from detected language."""

import sqlalchemy as sa

from alembic import op

revision = "0002_recording_language"
down_revision = "0001_transcription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_recording",
        sa.Column(
            "requested_language", sa.String(8), nullable=False, server_default="auto"
        ),
    )


def downgrade() -> None:
    op.drop_column("meeting_recording", "requested_language")
