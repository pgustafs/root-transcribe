"""Meeting notes: note agents (instructions) and the documents they write."""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op
from app.services.note_defaults import BUILTIN_AGENTS

revision = "0003_meeting_notes"
down_revision = "0002_recording_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    note_agent = op.create_table(
        "note_agent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("builtin_key", sa.String(length=50), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_note_agent_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_note_agent_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_note_agent")),
        sa.UniqueConstraint("builtin_key", name=op.f("uq_note_agent_builtin_key")),
    )
    op.create_index(
        op.f("ix_note_agent_owner_id"), "note_agent", ["owner_id"], unique=False
    )
    op.create_table(
        "meeting_note",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("agent_name", sa.String(length=120), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_step", sa.String(length=50), nullable=True),
        sa.Column("generated_markdown", sa.Text(), nullable=True),
        sa.Column("content_markdown", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_email", sa.String(length=255), nullable=True),
        sa.Column("edited_by_email", sa.String(length=255), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["note_agent.id"],
            name=op.f("fk_meeting_note_agent_id_note_agent"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_meeting_note_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["meeting_recording.id"],
            name=op.f("fk_meeting_note_recording_id_meeting_recording"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_meeting_note_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meeting_note")),
    )
    op.create_index(
        op.f("ix_meeting_note_recording_id"),
        "meeting_note",
        ["recording_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_meeting_note_status"), "meeting_note", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_meeting_note_workspace_id"),
        "meeting_note",
        ["workspace_id"],
        unique=False,
    )
    # The agents shipped with the app. Their text is a starting point: it can
    # be edited, and restored from services/note_defaults.py.
    now = datetime.now(UTC)
    op.bulk_insert(
        note_agent,
        [
            {
                "id": uuid.uuid4(),
                "owner_id": None,
                "builtin_key": agent.key,
                "name": agent.name,
                "description": agent.description,
                "instructions": agent.instructions,
                "is_active": True,
                "created_by_id": None,
                "created_at": now,
                "updated_at": now,
            }
            for agent in BUILTIN_AGENTS
        ],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_meeting_note_workspace_id"), table_name="meeting_note")
    op.drop_index(op.f("ix_meeting_note_status"), table_name="meeting_note")
    op.drop_index(op.f("ix_meeting_note_recording_id"), table_name="meeting_note")
    op.drop_table("meeting_note")
    op.drop_index(op.f("ix_note_agent_owner_id"), table_name="note_agent")
    op.drop_table("note_agent")
