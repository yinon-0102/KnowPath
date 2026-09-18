"""Store immutable original upload bytes separately from public metadata."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGBLOB

revision = "0013_material_raw"
down_revision = "0012_correction_context"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "material_raw_files",
        sa.Column("version_id", sa.String(36), sa.ForeignKey("material_versions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("content", sa.LargeBinary().with_variant(LONGBLOB(), "mysql"), nullable=False),
    )


def downgrade():
    op.drop_table("material_raw_files")
