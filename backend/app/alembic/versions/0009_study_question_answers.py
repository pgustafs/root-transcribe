"""Notebook entries: a marked question and its suggested answer."""

import sqlalchemy as sa

from alembic import op

revision = "0009_study_question_answers"
down_revision = "0008_study_note_enrichment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "study_session",
        sa.Column(
            "research_web", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("study_note", sa.Column("question", sa.Text(), nullable=True))
    op.add_column("study_note", sa.Column("answer_short", sa.Text(), nullable=True))
    op.add_column("study_note", sa.Column("answer", sa.Text(), nullable=True))
    op.add_column("study_note", sa.Column("answer_sources", sa.JSON(), nullable=True))
    op.add_column(
        "study_note",
        sa.Column(
            "answer_status", sa.String(20), nullable=False, server_default="none"
        ),
    )
    op.add_column(
        "study_note", sa.Column("answer_error", sa.String(2000), nullable=True)
    )
    op.add_column(
        "study_note",
        sa.Column("answer_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    for column in (
        "answer_count",
        "answer_error",
        "answer_status",
        "answer_sources",
        "answer",
        "answer_short",
        "question",
    ):
        op.drop_column("study_note", column)
    op.drop_column("study_session", "research_web")
