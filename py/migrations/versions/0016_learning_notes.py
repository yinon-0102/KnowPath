"""Cloud learning notebook aggregates."""
from alembic import op
from knowpath_backend.learning.persistence.db import NotebookRow, NoteChapterRow, NoteRevisionRow, NoteGenerationRow
revision = '0016_learning_notes'
down_revision = '0015_material_object_storage'
branch_labels = None
depends_on = None
def upgrade():
    for row in (NotebookRow, NoteChapterRow, NoteRevisionRow, NoteGenerationRow):
        row.__table__.create(op.get_bind(), checkfirst=True)
def downgrade():
    for row in (NoteGenerationRow, NoteRevisionRow, NoteChapterRow, NotebookRow):
        row.__table__.drop(op.get_bind(), checkfirst=True)
