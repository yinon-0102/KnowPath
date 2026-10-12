import pytest

from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.test.test_learning_plans_sessions import seed


def test_unknown_is_not_weak_and_preview_does_not_apply():
    state, space, _ = seed(InMemoryMaterialRepository())
    plan = state.create_plan(space, {})
    recommendation = state.policy_recommendations.create(space, {}, 'recommend')
    assert recommendation['action']['kind'] == 'diagnostic'
    assert '未知' in recommendation['explanation'] or '不足' in recommendation['explanation']
    preview = state.policy_recommendations.apply(recommendation['id'], {'preview': True, 'expected_plan_version': plan['version']}, 'preview')
    assert preview['can_apply']
    assert state.get_plan(plan['id'])['version'] == plan['version']
    assert not state.assessment_service.repository.records('assessments', space_id=space)


def test_recommendation_rejects_changed_plan():
    state, space, _ = seed(InMemoryMaterialRepository())
    plan = state.create_plan(space, {})
    recommendation = state.policy_recommendations.create(space, {}, 'recommend')
    state.update_plan_order(plan['id'], {'order_mode': 'custom', 'expected_plan_version': 1,
                                       'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]})
    with pytest.raises(DomainConflict, match='版本|依据'):
        state.policy_recommendations.apply(recommendation['id'], {'preview': False, 'expected_plan_version': 1}, 'apply')
