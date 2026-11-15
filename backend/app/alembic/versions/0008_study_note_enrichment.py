"""Notebook entries: tags, and what the language model adds to a note."""

import sqlalchemy as sa

from alembic import op

revision = "0008_study_note_enrichment"
down_revision = "0007_study_note_comment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "study_session",
        sa.Column("enrich", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "study_note",
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column("study_note", sa.Column("title", sa.String(200), nullable=True))
    op.add_column("study_note", sa.Column("clean_text", sa.Text(), nullable=True))
    op.add_column("study_note", sa.Column("slide_text", sa.Text(), nullable=True))
    op.add_column("study_note", sa.Column("terms", sa.JSON(), nullable=True))
    op.add_column("study_note", sa.Column("explanation", sa.Text(), nullable=True))
    op.add_column(
        "study_note",
        sa.Column(
            "enrich_status", sa.String(20), nullable=False, server_default="none"
        ),
    )
    op.add_column(
        "study_note", sa.Column("enrich_error", sa.String(2000), nullable=True)
    )
    op.add_column(
        "study_note",
        sa.Column("enrich_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    for column in (
        "enrich_count",
        "enrich_error",
        "enrich_status",
        "explanation",
        "terms",
        "slide_text",
        "clean_text",
        "title",
        "tags",
    ):
        op.drop_column("study_note", column)
    op.drop_column("study_session", "enrich")
