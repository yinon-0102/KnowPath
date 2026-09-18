"""Persist ordered run events independently of the final run status."""

from alembic import op
import sqlalchemy as sa

revision = "0002_run_events"
down_revision = "0001_learning_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_events_created_at", "run_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_run_events_created_at", table_name="run_events")
    op.drop_table("run_events")
