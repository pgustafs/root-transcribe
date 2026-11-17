"""What a recording's channels hold, and how many people are at the microphone."""

import sqlalchemy as sa

from alembic import op

revision = "0012_recording_audio_layout"
down_revision = "0010_recording_speakers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_recording",
        sa.Column(
            "audio_layout", sa.String(16), nullable=False, server_default="mixed"
        ),
    )
    op.add_column(
        "meeting_recording", sa.Column("mic_speakers", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("meeting_recording", "mic_speakers")
    op.drop_column("meeting_recording", "audio_layout")
