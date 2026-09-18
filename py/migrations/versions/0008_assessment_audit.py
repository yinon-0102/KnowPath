"""Persist assessment review audit context without altering frozen snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "0008_assessment_audit"
down_revision = "0007_material_version"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assessments", sa.Column("context", sa.JSON(), nullable=True))
    assessment = sa.table("assessments", sa.column("context", sa.JSON()))
    op.execute(assessment.update().values(context={}))
    with op.batch_alter_table("assessments") as batch:
        batch.alter_column("context", existing_type=sa.JSON(), nullable=False)


def downgrade():
    op.drop_column("assessments", "context")
