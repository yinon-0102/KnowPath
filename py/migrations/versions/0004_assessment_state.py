"""Persist assessment snapshots, answer revisions and evidence cycles."""
from alembic import op
import sqlalchemy as sa

revision = "0004_assessment_state"
down_revision = "0003_space_profiles"
branch_labels = None
depends_on = None


def upgrade():
    for table, column in (("assessments", "snapshot"), ("evidence", "context"), ("learner_states", "context")):
        op.add_column(table, sa.Column(column, sa.JSON(), nullable=True))
        entity = sa.table(table, sa.column(column, sa.JSON()))
        op.get_bind().execute(entity.update().values({column: {}}))
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=sa.JSON(), nullable=False)
    for name in ("run_id", "finalize_run_id", "submission_id"):
        op.add_column("assessments", sa.Column(name, sa.String(36), nullable=True))
    op.add_column("attempts", sa.Column("elapsed_seconds", sa.Integer(), nullable=True))
    with op.batch_alter_table("attempts") as batch:
        batch.create_unique_constraint("uq_attempt_revision", ["assessment_id", "question_id", "answer_revision"])
    with op.batch_alter_table("learner_states") as batch:
        batch.create_unique_constraint("uq_learner_space_topic", ["space_id", "topic_id"])
    op.create_table("state_resets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("topic_ids", sa.JSON(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_state_resets_space_id", "state_resets", ["space_id"])


def downgrade():
    op.drop_table("state_resets")
    with op.batch_alter_table("attempts") as batch:
        batch.drop_constraint("uq_attempt_revision", type_="unique")
        batch.drop_column("elapsed_seconds")
    with op.batch_alter_table("learner_states") as batch:
        batch.drop_constraint("uq_learner_space_topic", type_="unique")
        batch.drop_column("context")
    with op.batch_alter_table("assessments") as batch:
        for name in ("snapshot", "run_id", "finalize_run_id", "submission_id"):
            batch.drop_column(name)
    op.drop_column("evidence", "context")
