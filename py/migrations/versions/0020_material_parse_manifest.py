"""Persist per-page coverage and immutable teaching structure for new parses."""
from alembic import op
import sqlalchemy as sa

revision = '0020_material_parse_manifest'
down_revision = '0019_material_parser_version'
branch_labels = None
depends_on = None


def upgrade():
    # NULL explicitly means the legacy projection; never rederive old identity.
    op.add_column('material_versions', sa.Column('parse_manifest', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('material_versions', 'parse_manifest')
