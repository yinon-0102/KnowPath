"""Persist study-plan revisions, task notes and session event state."""
from alembic import op
import sqlalchemy as sa

revision = "0005_plans_sessions"
down_revision = "0004_assessment_state"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("study_plans", sa.Column("scope_version", sa.Integer(), nullable=True))
    op.add_column("study_plans", sa.Column("run_id", sa.String(36), nullable=True))
    op.add_column("study_plans", sa.Column("config", sa.JSON(), nullable=True))
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE study_plans SET scope_version = 0 WHERE scope_version IS NULL"))
    bind.execute(sa.text("UPDATE study_plans SET config = '{}' WHERE config IS NULL"))
    with op.batch_alter_table("study_plans") as batch:
        batch.alter_column("scope_version", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("config", existing_type=sa.JSON(), nullable=False)
    for table in ("study_tasks", "learning_sessions"):
        op.add_column(table, sa.Column("context", sa.JSON(), nullable=True))
        entity = sa.table(table, sa.column("context", sa.JSON()))
        bind.execute(entity.update().values(context={}))
        with op.batch_alter_table(table) as batch:
            batch.alter_column("context", existing_type=sa.JSON(), nullable=False)
    op.add_column("learning_sessions", sa.Column("active_space_id", sa.String(36), nullable=True))
    # Refuse ambiguous legacy data rather than silently finishing an active session.
    bind.execute(sa.text("UPDATE learning_sessions SET active_space_id = space_id WHERE status = 'active'"))
    with op.batch_alter_table("learning_sessions") as batch:
        batch.create_unique_constraint("uq_learning_sessions_active_space_id", ["active_space_id"])
    op.add_column("study_tasks", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("study_tasks", sa.Column("defer_until", sa.DateTime(timezone=True), nullable=True))

def downgrade():
    with op.batch_alter_table("learning_sessions") as batch:
        batch.drop_constraint("uq_learning_sessions_active_space_id", type_="unique")
        batch.drop_column("active_space_id")
        batch.drop_column("context")
    op.drop_column("study_tasks", "context")
    with op.batch_alter_table("study_tasks") as batch:
        batch.drop_column("defer_until")
        batch.drop_column("note")
    with op.batch_alter_table("study_plans") as batch:
        batch.drop_column("config")
        batch.drop_column("run_id")
        batch.drop_column("scope_version")
