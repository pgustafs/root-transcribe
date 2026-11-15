"""Study notes: notebooks, sessions, captures and the shortcut credential."""

import sqlalchemy as sa

from alembic import op

revision = "0005_study_notes"
down_revision = "0004_speaker_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hotkey_credential",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("hint", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_hotkey_credential_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hotkey_credential")),
        sa.UniqueConstraint("user_id", name=op.f("uq_hotkey_credential_user_id")),
    )
    op.create_index(
        op.f("ix_hotkey_credential_token_hash"),
        "hotkey_credential",
        ["token_hash"],
        unique=True,
    )
    op.create_table(
        "study_notebook",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=True),
        sa.Column("source_key", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_study_notebook_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_study_notebook_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_study_notebook")),
        sa.UniqueConstraint(
            "owner_id", "workspace_id", "source_key", name="uq_study_notebook_source"
        ),
    )
    op.create_index(
        op.f("ix_study_notebook_owner_id"), "study_notebook", ["owner_id"], unique=False
    )
    op.create_index(
        op.f("ix_study_notebook_workspace_id"),
        "study_notebook",
        ["workspace_id"],
        unique=False,
    )
    op.create_table(
        "study_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notebook_id", sa.Uuid(), nullable=False),
        sa.Column("recording_id", sa.Uuid(), nullable=True),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["notebook_id"],
            ["study_notebook.id"],
            name=op.f("fk_study_session_notebook_id_study_notebook"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_study_session")),
    )
    op.create_index(
        op.f("ix_study_session_notebook_id"),
        "study_session",
        ["notebook_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_study_session_recording_id"),
        "study_session",
        ["recording_id"],
        unique=False,
    )
    op.create_table(
        "study_note",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notebook_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("captured_ms", sa.Integer(), nullable=False),
        sa.Column("audio_start_ms", sa.Integer(), nullable=True),
        sa.Column("audio_end_ms", sa.Integer(), nullable=True),
        sa.Column("image_key", sa.String(length=512), nullable=True),
        sa.Column("image_width", sa.Integer(), nullable=True),
        sa.Column("image_height", sa.Integer(), nullable=True),
        sa.Column("audio_key", sa.String(length=512), nullable=True),
        sa.Column("has_audio", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("end_reason", sa.String(length=30), nullable=True),
        sa.Column("source_time_url", sa.String(length=2000), nullable=True),
        sa.Column("send_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["notebook_id"],
            ["study_notebook.id"],
            name=op.f("fk_study_note_notebook_id_study_notebook"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["study_session.id"],
            name=op.f("fk_study_note_session_id_study_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_study_note")),
        sa.UniqueConstraint("session_id", "seq", name="uq_study_note_session_seq"),
    )
    op.create_index(
        op.f("ix_study_note_notebook_id"), "study_note", ["notebook_id"], unique=False
    )
    op.create_index(
        op.f("ix_study_note_session_id"), "study_note", ["session_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_study_note_session_id"), table_name="study_note")
    op.drop_index(op.f("ix_study_note_notebook_id"), table_name="study_note")
    op.drop_table("study_note")
    op.drop_index(op.f("ix_study_session_recording_id"), table_name="study_session")
    op.drop_index(op.f("ix_study_session_notebook_id"), table_name="study_session")
    op.drop_table("study_session")
    op.drop_index(op.f("ix_study_notebook_workspace_id"), table_name="study_notebook")
    op.drop_index(op.f("ix_study_notebook_owner_id"), table_name="study_notebook")
    op.drop_table("study_notebook")
    op.drop_index(
        op.f("ix_hotkey_credential_token_hash"), table_name="hotkey_credential"
    )
    op.drop_table("hotkey_credential")
