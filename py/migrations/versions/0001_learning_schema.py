"""Create the frozen initial learning-domain schema.

Keep this revision independent of the current ORM models. Later schema changes
belong in new revisions, so a database at 0001 always has the same structure.
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_learning_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "materials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_version_id", sa.String(36), nullable=True),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "material_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("material_id", sa.String(36), sa.ForeignKey("materials.id"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("graph_version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_material_versions_material_id", "material_versions", ["material_id"])
    op.create_index("ix_material_versions_content_hash", "material_versions", ["content_hash"])
    op.create_table(
        "source_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("material_version_id", sa.String(36), sa.ForeignKey("material_versions.id"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("section_path", sa.JSON, nullable=False),
        sa.Column("page", sa.Integer, nullable=True),
        sa.Column("line_start", sa.Integer, nullable=True),
        sa.Column("line_end", sa.Integer, nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_source_chunks_material_version_id", "source_chunks", ["material_version_id"])
    op.create_index("ix_source_chunks_content_hash", "source_chunks", ["content_hash"])
    op.create_table(
        "topics",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("material_version_id", sa.String(36), sa.ForeignKey("material_versions.id"), nullable=False),
        sa.Column("revision_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("source_chunk_ids", sa.JSON, nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_topics_material_version_id", "topics", ["material_version_id"])
    op.create_table(
        "knowledge_relations",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("revision_id", sa.String(128), nullable=False),
        sa.Column("from_topic_id", sa.String(128), nullable=False),
        sa.Column("to_topic_id", sa.String(128), nullable=False),
        sa.Column("relation_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_refs", sa.JSON, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_relations_from_topic_id", "knowledge_relations", ["from_topic_id"])
    op.create_index("ix_knowledge_relations_to_topic_id", "knowledge_relations", ["to_topic_id"])
    op.create_table(
        "learning_spaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("goal", sa.Text, nullable=True),
        sa.Column("target_date", sa.String(32), nullable=True),
        sa.Column("weekly_minutes", sa.Integer, nullable=True),
        sa.Column("bindings", sa.JSON, nullable=False),
        sa.Column("topic_ids", sa.JSON, nullable=False),
        sa.Column("excluded_topic_ids", sa.JSON, nullable=False),
        sa.Column("space_version", sa.Integer, nullable=False),
        sa.Column("scope_version", sa.Integer, nullable=False),
        sa.Column("profile_version", sa.Integer, nullable=False),
        sa.Column("state_version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "learner_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("topic_id", sa.String(128), nullable=False),
        sa.Column("mastery_score", sa.Float, nullable=True),
        sa.Column("score_validity", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("evidence_ids", sa.JSON, nullable=False),
        sa.Column("error_tags", sa.JSON, nullable=False),
        sa.Column("state_version", sa.Integer, nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("last_assessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_learner_states_space_id", "learner_states", ["space_id"])
    op.create_index("ix_learner_states_topic_id", "learner_states", ["topic_id"])
    op.create_table(
        "assessments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("topic_ids", sa.JSON, nullable=False),
        sa.Column("questions", sa.JSON, nullable=False),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_assessments_space_id", "assessments", ["space_id"])
    op.create_table(
        "attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("assessment_id", sa.String(36), sa.ForeignKey("assessments.id"), nullable=False),
        sa.Column("question_id", sa.String(128), nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("answer_revision", sa.Integer, nullable=False),
        sa.Column("assisted", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_attempts_assessment_id", "attempts", ["assessment_id"])
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("topic_id", sa.String(128), nullable=False),
        sa.Column("assessment_id", sa.String(36), sa.ForeignKey("assessments.id"), nullable=True),
        sa.Column("attempt_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("error_tags", sa.JSON, nullable=False),
        sa.Column("source_refs", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_space_id", "evidence", ["space_id"])
    op.create_index("ix_evidence_topic_id", "evidence", ["topic_id"])
    op.create_table(
        "study_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_study_plans_space_id", "study_plans", ["space_id"])
    op.create_table(
        "study_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("study_plans.id"), nullable=False),
        sa.Column("topic_ids", sa.JSON, nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("estimated_minutes", sa.Integer, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
    )
    op.create_index("ix_study_tasks_plan_id", "study_tasks", ["plan_id"])
    op.create_table(
        "runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("progress", sa.Integer, nullable=False),
        sa.Column("result_ref", sa.JSON, nullable=True),
        sa.Column("error", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "learning_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("study_plans.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("study_tasks.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_learning_sessions_space_id", "learning_sessions", ["space_id"])
    op.create_table(
        "session_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("learning_sessions.id"), nullable=False),
        sa.Column("client_event_id", sa.String(128), nullable=True, unique=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_session_events_session_id", "session_events", ["session_id"])
    op.create_table(
        "knowledge_corrections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("proposed_value", sa.JSON, nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_corrections_space_id", "knowledge_corrections", ["space_id"])
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("version_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("outbox_events")
    op.drop_table("knowledge_corrections")
    op.drop_table("session_events")
    op.drop_table("learning_sessions")
    op.drop_table("runs")
    op.drop_table("study_tasks")
    op.drop_table("study_plans")
    op.drop_table("evidence")
    op.drop_table("attempts")
    op.drop_table("assessments")
    op.drop_table("learner_states")
    op.drop_table("learning_spaces")
    op.drop_table("knowledge_relations")
    op.drop_table("topics")
    op.drop_table("source_chunks")
    op.drop_table("material_versions")
    op.drop_table("materials")
