"""Persist graph candidates and their exact source/diff snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "0010_graph_revisions"
down_revision = "0009_messages"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("graph_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("material_id", sa.String(36), sa.ForeignKey("materials.id"), nullable=False),
        sa.Column("material_version_id", sa.String(36), sa.ForeignKey("material_versions.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("base_graph_version", sa.Integer(), nullable=False),
        sa.Column("graph_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("diff", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("material_id", "sequence", name="uq_graph_revision_sequence"),
        sa.UniqueConstraint("material_id", "graph_version", name="uq_graph_published_version"))
    op.create_index("ix_graph_revisions_material_id", "graph_revisions", ["material_id"])


def downgrade():
    op.drop_table("graph_revisions")
