"""Durable budget previews, adjustments and policy recommendations."""
from alembic import op
from knowpath_backend.learning.persistence.db import BudgetComparisonRow, PlanAdjustmentRow, PolicyRecommendationRow

revision = '0018_learning_personalization'
down_revision = '0017_study_lessons'
branch_labels = None
depends_on = None


def upgrade():
    for model in (BudgetComparisonRow, PlanAdjustmentRow, PolicyRecommendationRow):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in (PolicyRecommendationRow, PlanAdjustmentRow, BudgetComparisonRow):
        model.__table__.drop(op.get_bind(), checkfirst=True)
