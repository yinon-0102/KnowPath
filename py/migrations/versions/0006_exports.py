"""Persist immutable, expiring learning export snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "0006_exports"
down_revision = "0005_plans_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_exports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_learning_exports_space_id", "learning_exports", ["space_id"])
    op.create_index("ix_learning_exports_expires_at", "learning_exports", ["expires_at"])


def downgrade():
    op.drop_table("learning_exports")
