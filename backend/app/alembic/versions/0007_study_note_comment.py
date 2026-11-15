"""Notebook entries: the owner's own words, apart from the transcript."""

import sqlalchemy as sa

from alembic import op

revision = "0007_study_note_comment"
down_revision = "0006_drop_hotkey_credential"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("study_note", sa.Column("comment", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("study_note", "comment")
