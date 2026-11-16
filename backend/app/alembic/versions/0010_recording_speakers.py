"""How many people speak in a recording, when the user knows (NULL: detect)."""

import sqlalchemy as sa

from alembic import op

revision = "0010_recording_speakers"
down_revision = "0009_study_question_answers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_recording",
        sa.Column("expected_speakers", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meeting_recording", "expected_speakers")
