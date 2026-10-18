"""Persist scope mode, prerequisite preference and automatic inclusion reasons."""
from alembic import op
import sqlalchemy as sa

revision = '0021_space_scope_options'
down_revision = '0020_material_parse_manifest'
branch_labels = None
depends_on = None


def upgrade():
    # NULL preserves the original semantics of historical scopes without
    # rewriting any scope, plan, question or mastery evidence.
    op.add_column('learning_spaces', sa.Column('scope_options', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('learning_spaces', 'scope_options')
