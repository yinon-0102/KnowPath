"""Graph preparation receipts, publication audits and fenced outbox leases."""
from alembic import op
import sqlalchemy as sa

revision = "0011_graph_preparation"
down_revision = "0010_graph_revisions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("graph_revisions", sa.Column("preparation", sa.JSON(), nullable=True))
    op.add_column("graph_revisions", sa.Column("publication", sa.JSON(), nullable=True))
    op.add_column("outbox_events", sa.Column("lease_token", sa.String(36), nullable=True))
    op.add_column("outbox_events", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("available_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))


def downgrade():
    with op.batch_alter_table("outbox_events") as batch:
        for name in ("attempts", "available_at", "lease_until", "lease_token"):
            batch.drop_column(name)
    with op.batch_alter_table("graph_revisions") as batch:
        batch.drop_column("publication")
        batch.drop_column("preparation")
