"""Persist explicit space profiles and transactional command replay responses."""
from datetime import timezone

from alembic import op
import sqlalchemy as sa

revision = "0003_space_profiles"
down_revision = "0002_run_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("learning_spaces", sa.Column("profile", sa.JSON(), nullable=True))
    op.add_column("idempotency_keys", sa.Column("response", sa.JSON(), nullable=True))
    spaces = sa.table("learning_spaces", sa.column("id", sa.String()),
                      sa.column("goal", sa.Text()), sa.column("weekly_minutes", sa.Integer()),
                      sa.column("target_date", sa.String()), sa.column("updated_at", sa.DateTime()),
                      sa.column("profile", sa.JSON()))
    connection = op.get_bind()
    for row in connection.execute(sa.select(spaces)).mappings().all():
        timestamp = row["updated_at"].replace(tzinfo=timezone.utc).isoformat()
        profile = {key: {"value": row[key], "source": "explicit", "updated_at": timestamp}
                   for key in ("goal", "weekly_minutes", "target_date")}
        connection.execute(spaces.update().where(spaces.c.id == row["id"]).values(profile=profile))
    with op.batch_alter_table("learning_spaces") as batch:
        batch.alter_column("profile", existing_type=sa.JSON(), nullable=False)


def downgrade():
    with op.batch_alter_table("learning_spaces") as batch:
        batch.drop_column("profile")
    op.drop_column("idempotency_keys", "response")
