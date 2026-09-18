"""Persist correction source, candidate and confirmation audit context."""
from alembic import op
import sqlalchemy as sa
revision = '0012_correction_context'
down_revision = '0011_graph_preparation'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('knowledge_corrections', sa.Column('context', sa.JSON(), nullable=True))
    table = sa.table('knowledge_corrections', sa.column('context', sa.JSON()))
    op.execute(table.update().values(context={}))
    with op.batch_alter_table('knowledge_corrections') as batch:
        batch.alter_column('context', existing_type=sa.JSON(), nullable=False)


def downgrade():
    with op.batch_alter_table('knowledge_corrections') as batch:
        batch.drop_column('context')
