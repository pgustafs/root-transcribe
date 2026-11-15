"""Study notes: remove the keyboard-shortcut credential (the feature is gone)."""

import sqlalchemy as sa

from alembic import op

revision = "0006_drop_hotkey_credential"
down_revision = "0005_study_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        op.f("ix_hotkey_credential_token_hash"), table_name="hotkey_credential"
    )
    op.drop_table("hotkey_credential")


def downgrade() -> None:
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
