"""Optimistic metadata version for materials; backfill existing rows to one."""
from alembic import op
import sqlalchemy as sa

revision = "0007_material_version"
down_revision = "0006_exports"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("materials", sa.Column("version", sa.Integer(), server_default="1", nullable=False))


def downgrade():
    op.drop_column("materials", "version")
