"""create learning domain schema"""

from alembic import op

from my_agent_llms.learning.db import Base


revision = "0001_learning_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
