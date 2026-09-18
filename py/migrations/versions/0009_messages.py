"""Persist independent conversations and source-bound messages."""
from alembic import op
import sqlalchemy as sa

revision = "0009_messages"
down_revision = "0008_assessment_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("learning_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("learning_session_id", sa.String(36), sa.ForeignKey("learning_sessions.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_learning_conversations_space_id", "learning_conversations", ["space_id"])
    op.create_table("learning_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("learning_conversations.id"), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_message_sequence"))
    op.create_index("ix_learning_messages_space_id", "learning_messages", ["space_id"])
    op.create_index("ix_learning_messages_conversation_id", "learning_messages", ["conversation_id"])


def downgrade():
    op.drop_table("learning_messages")
    op.drop_table("learning_conversations")
