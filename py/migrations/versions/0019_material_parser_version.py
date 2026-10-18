"""Record parser identity without rewriting historical parsed sources."""
from alembic import op
import sqlalchemy as sa

revision = "0019_material_parser_version"
down_revision = "0018_learning_personalization"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("material_versions", sa.Column("parser_version", sa.String(128), nullable=False, server_default="legacy_unknown"))


def downgrade():
    op.drop_column("material_versions", "parser_version")
