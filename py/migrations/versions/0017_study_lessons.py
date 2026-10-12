"""Persist generated lesson versions."""
from alembic import op
from knowpath_backend.learning.persistence.db import StudyLessonRow
revision = '0017_study_lessons'
down_revision = '0016_learning_notes'
branch_labels = None
depends_on = None

def upgrade():
    StudyLessonRow.__table__.create(op.get_bind(), checkfirst=True)

def downgrade():
    StudyLessonRow.__table__.drop(op.get_bind(), checkfirst=True)
