"""Ordering changes presentation and schedule, never learning identity."""
from copy import deepcopy
import pytest
from sqlalchemy import create_engine
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState
from knowpath_backend.test.test_learning_plans_sessions import seed

@pytest.fixture(params=['memory', 'sqlite'])
def ordered_backend(request, tmp_path):
    if request.param == 'memory':
        return InMemoryMaterialRepository()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'order.db'}")
    init_db(engine)
    return SqlAlchemyMaterialRepository(engine)

def test_initial_default_follows_source_not_topic_hash(ordered_backend):
    state, space_id, _ = seed(ordered_backend)
    plan = state.create_plan(space_id, {})
    lines = [t['context']['source_refs'][0]['line_start'] for t in plan['tasks']]
    assert lines == sorted(lines)
    assert plan['config']['order_mode'] == 'source'
    assert plan['config']['order_policy_version'] == 'order-v1'


def test_new_default_canonical_scope_has_one_task_per_unit_in_all_order_modes(ordered_backend):
    state = LearningState(ordered_backend)
    material = state.material_service.create(filename='units.md', content=b'# Chapter\n\nIntroduction.\n\n## A\n\nA fact.\n\n## B\n\nB fact.', idempotency_key='default-units')
    space = state.create_space({'material_ids': [material.material.id], 'weekly_minutes': 120})
    assert space['scope_mode'] == 'all'
    assert len(space['topic_ids']) == 3
    for mode in ('source', 'adaptive'):
        plan = state.create_plan(space['id'], {'order_mode': mode})
        assert len(plan['tasks']) == 3
        assert {t['topic_ids'][0] for t in plan['tasks']} == set(space['topic_ids'])
    reordered = state.update_plan_order(plan['id'], {'expected_plan_version': 1, 'order_mode': 'custom',
        'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]})
    assert len(reordered['tasks']) == 3
    assert {t['topic_ids'][0] for t in reordered['tasks']} == set(space['topic_ids'])


def test_selected_legacy_alias_still_orders_before_its_canonical_dependent():
    from knowpath_backend.learning.plans.ordering import order_topics
    a = {'id': 'a', 'name': 'A', 'source_refs': [{'material_id': 'm', 'line_start': 2}]}
    alias = dict(a, id='alias_a', canonical_topic_id='a')
    b = {'id': 'b', 'name': 'B', 'prerequisites': ['a'], 'source_refs': [{'material_id': 'm', 'line_start': 1}]}
    ordered = order_topics([alias, b], {}, 'source', ['m'], '2026-10-18T00:00:00+00:00', all_topics=[a, alias, b])
    assert [t['id'] for t in ordered] == ['alias_a', 'b']


@pytest.mark.parametrize('validity', ['valid', 'stale', 'missing_revision', 'wrong_revision'])
def test_custom_preview_and_apply_use_current_revision_mastery_for_prerequisites(validity, monkeypatch):
    from knowpath_backend.learning.assessments.service import revision
    state, space_id, _ = seed(InMemoryMaterialRepository())
    bound = state.space_service.bound_topics
    def dependencies(space):
        topics = bound(space)
        canonical = [t for t in topics if t['id'] == t['canonical_topic_id']]
        for t in topics:
            if t['canonical_topic_id'] == canonical[-1]['id']: t['prerequisites'] = [canonical[0]['id']]
        return topics
    monkeypatch.setattr(state.space_service, 'bound_topics', dependencies)
    plan = state.create_plan(space_id, {})
    parent = next(t for t in dependencies(state.space_service.get(space_id)) if t['id'] == plan['tasks'][0]['topic_ids'][0])
    row = {'id': 'mastery', 'space_id': space_id, 'topic_id': parent['id'], 'status': 'mastered',
           'score_validity': 'stale' if validity == 'stale' else 'current', 'topic_revision_id': revision(parent)}
    if validity == 'missing_revision': row.pop('topic_revision_id')
    if validity == 'wrong_revision': row['topic_revision_id'] = 'old-revision'
    state.graph_service.repository.put_record('states', row)
    payload = {'expected_plan_version': 1, 'order_mode': 'custom', 'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]}
    preview = state.preview_plan_order(plan['id'], payload)
    assert preview['can_apply'] is (validity == 'valid')
    if validity == 'valid':
        assert state.update_plan_order(plan['id'], payload)['version'] == 2
    else:
        with pytest.raises(DomainConflict): state.update_plan_order(plan['id'], payload)

def test_custom_preview_is_readonly_and_apply_preserves_identity(ordered_backend):
    state, space_id, _ = seed(ordered_backend)
    plan = state.create_plan(space_id, {})
    payload = {'expected_plan_version': 1, 'order_mode': 'custom',
               'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]}
    before = deepcopy(state.get_plan(plan['id']))
    preview = state.preview_plan_order(plan['id'], payload)
    assert preview['can_apply'] is True
    assert state.get_plan(plan['id']) == before
    applied = state.update_plan_order(plan['id'], payload, idempotency_key='order-1')
    assert applied['version'] == 2
    assert [t['node_id'] for t in applied['tasks']] == payload['ordered_node_ids']
    for old in plan['tasks']:
        new = next(t for t in applied['tasks'] if t['id'] == old['id'])
        assert new['sequence'] == old['sequence']
        assert new['context']['title'] == old['context']['title']
        assert new['context']['source_refs'] == old['context']['source_refs']
    assert state.update_plan_order(plan['id'], payload, idempotency_key='order-1') == applied
    restarted = LearningState(ordered_backend)
    assert restarted.get_plan(plan['id'])['tasks'] == applied['tasks']

def test_custom_invalid_permutation_rolls_back(ordered_backend):
    state, space_id, _ = seed(ordered_backend)
    plan = state.create_plan(space_id, {})
    payload = {'expected_plan_version': 1, 'order_mode': 'custom', 'ordered_node_ids': []}
    with pytest.raises(DomainConflict) as exc:
        state.update_plan_order(plan['id'], payload)
    assert exc.value.code == 'INVALID_ORDER_PERMUTATION'
    assert state.get_plan(plan['id']) == plan

def test_active_session_pending_task_is_fixed(ordered_backend):
    state, space_id, _ = seed(ordered_backend)
    plan = state.create_plan(space_id, {})
    started = state.start_session(plan['id'], plan['tasks'][0]['id'])
    current = state.get_plan(plan['id'])
    movable = current['tasks'][1:]
    preview = state.preview_plan_order(plan['id'], {
        'expected_plan_version': current['version'], 'order_mode': 'custom',
        'ordered_node_ids': [t['node_id'] for t in reversed(movable)]})
    assert preview['fixed_nodes'][0]['reason'] == 'active_session'
    assert preview['candidate_plan']['tasks'][0] == current['tasks'][0]
    assert state.get_workbench(space_id)['active_session']['id'] == started['id']

def test_custom_preference_inherited_by_local_replan(ordered_backend):
    state, space_id, _ = seed(ordered_backend)
    plan = state.create_plan(space_id, {})
    ordered = state.update_plan_order(plan['id'], {'expected_plan_version': 1, 'order_mode': 'custom',
        'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]})
    rebuilt = state.create_plan(space_id, {'rebuild_mode': 'local_replan', 'base_plan_id': plan['id'],
                                            'expected_plan_version': ordered['version']})
    assert rebuilt['config']['order_mode'] == 'custom'
    assert [t['node_id'] for t in rebuilt['tasks']] == [t['node_id'] for t in ordered['tasks']]

def test_adaptive_prioritizes_weakness_and_source_breaks_ties(ordered_backend, monkeypatch):
    state, space_id, topics = seed(ordered_backend, legacy=True)
    target = topics[-1]['id']
    monkeypatch.setattr(state.plan_sessions, '_planning_states', lambda *args, **kwargs: {target: {'status': 'unstable'}})
    source = state.create_plan(space_id, {})
    assert source['tasks'][0]['context']['source_refs'][0]['line_start'] == 3
    adaptive = state.create_plan(space_id, {'order_mode': 'adaptive'})
    assert adaptive['tasks'][0]['topic_ids'] == [target]
    tied = adaptive['tasks'][1:]
    lines = [t['context']['source_refs'][0]['line_start'] for t in tied]
    assert lines == sorted(lines)


def test_custom_prerequisite_conflict_preview_and_rollback(ordered_backend, monkeypatch):
    state, space_id, _ = seed(ordered_backend, legacy=True)
    bound = state.space_service.bound_topics
    def prerequisites(space):
        topics = bound(space)
        topics[-1]['prerequisites'] = [topics[0]['id']]
        return topics
    monkeypatch.setattr(state.space_service, 'bound_topics', prerequisites)
    plan = state.create_plan(space_id, {})
    payload = {'expected_plan_version': 1, 'order_mode': 'custom',
               'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]}
    preview = state.preview_plan_order(plan['id'], payload)
    assert preview['can_apply'] is False
    assert preview['conflict_code'] == 'ORDER_PREREQUISITE_CONFLICT'
    with pytest.raises(DomainConflict) as exc:
        state.update_plan_order(plan['id'], payload)
    assert exc.value.code == 'ORDER_PREREQUISITE_CONFLICT'
    assert state.get_plan(plan['id']) == plan


def test_version_conflict_and_idempotency_survive_sql_restart(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'replay-order.db'}")
    init_db(engine)
    repo = SqlAlchemyMaterialRepository(engine)
    state, space_id, _ = seed(repo)
    plan = state.create_plan(space_id, {})
    payload = {'expected_plan_version': 1, 'order_mode': 'custom', 'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]}
    applied = state.update_plan_order(plan['id'], payload, idempotency_key='restart-order')
    second = LearningState(SqlAlchemyMaterialRepository(engine))
    assert second.update_plan_order(plan['id'], payload, idempotency_key='restart-order') == applied
    with pytest.raises(DomainConflict) as exc:
        second.update_plan_order(plan['id'], payload)
    assert exc.value.code == 'VERSION_CONFLICT'
    with pytest.raises(DomainConflict) as exc:
        second.update_plan_order(plan['id'], {**payload, 'order_mode': 'source', 'ordered_node_ids': None}, idempotency_key='restart-order')
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    engine.dispose()


def test_multiple_materials_follow_posted_and_explicit_order(ordered_backend):
    state = LearningState(ordered_backend)
    a = state.material_service.create(filename='a.md', content=b'# First\n\nFirst source.', idempotency_key='source-a').material.id
    b = state.material_service.create(filename='b.md', content=b'# Second\n\nSecond source.', idempotency_key='source-b').material.id
    material_ids = sorted([a, b], reverse=True)
    space = state.create_space({'name': 'Posted order', 'material_ids': material_ids})
    assert [binding['material_id'] for binding in space['bindings']] == material_ids
    plan = state.create_plan(space['id'], {})
    assert plan['tasks'][0]['context']['source_refs'][0]['material_id'] == material_ids[0]
    changed = state.update_plan_order(plan['id'], {'expected_plan_version': 1, 'order_mode': 'source', 'material_order': list(reversed(material_ids))})
    assert changed['tasks'][0]['context']['source_refs'][0]['material_id'] == material_ids[1]
    with pytest.raises(DomainConflict) as exc:
        state.preview_plan_order(plan['id'], {'expected_plan_version': 2, 'order_mode': 'source', 'material_order': [a]})
    assert exc.value.code == 'INVALID_MATERIAL_ORDER'


def test_completed_skipped_and_future_deferred_preserve_metadata(ordered_backend):
    state, space_id, _ = seed(ordered_backend, legacy=True)
    plan = state.create_plan(space_id, {})
    tasks = plan['tasks']
    state.update_task(plan['id'], tasks[0]['id'], {'expected_plan_version': 1, 'status': 'completed', 'note': 'keep note'})
    state.update_task(plan['id'], tasks[1]['id'], {'expected_plan_version': 2, 'status': 'skipped', 'reason': 'keep skip'})
    state.update_task(plan['id'], tasks[2]['id'], {'expected_plan_version': 3, 'status': 'deferred', 'defer_until': '2099-01-01T00:00:00+00:00'})
    current = state.get_plan(plan['id'])
    result = state.update_plan_order(plan['id'], {'expected_plan_version': 4, 'order_mode': 'custom',
                    'ordered_node_ids': [tasks[3]['node_id'], tasks[2]['node_id']]})
    assert result['tasks'][:2] == current['tasks'][:2]
    deferred = next(t for t in result['tasks'] if t['id'] == tasks[2]['id'])
    assert deferred['defer_until'] == '2099-01-01T00:00:00+00:00'
    assert 'session_index' not in deferred['context']
    assert 'scheduled_date' not in deferred['context']


def test_source_kahn_keeps_validated_prerequisites_and_labels_unknown():
    from knowpath_backend.learning.plans.ordering import order_topics, source_label
    topics = [{'id': 'later', 'name': 'Later', 'source_refs': [{'material_id': 'm', 'line_start': 20}], 'prerequisites': []},
              {'id': 'early', 'name': 'Early', 'source_refs': [{'material_id': 'm', 'line_start': 2}], 'prerequisites': ['later']},
              {'id': 'unknown', 'name': 'Unknown', 'source_refs': [], 'prerequisites': []}]
    ordered = order_topics(topics, {}, 'source', ['m'], '2026-10-10T00:00:00+00:00')
    assert [t['id'] for t in ordered] == ['later', 'early', 'unknown']
    assert source_label(topics[-1]) == '原文位置未记录'


def test_capacity_and_target_date_failure_are_readonly(ordered_backend):
    # Existing limits are immutable during reordering; only task order changes.
    from knowpath_backend.learning.plans.policy import schedule_tasks
    tasks = [{'id': str(i), 'status': 'pending', 'estimated_minutes': minutes, 'context': {}} for i, minutes in enumerate([20, 20, 10, 10, 20, 10])]
    config = {'session_count': 3, 'minutes_per_session': 30}
    space = {'weekly_minutes': 90, 'target_date': '2026-10-12'}
    from datetime import date
    with pytest.raises(DomainConflict):
        schedule_tasks(deepcopy(tasks), config, space, date(2026, 10, 10))
    scheduled = schedule_tasks(deepcopy([tasks[i] for i in [0, 2, 1, 3, 4, 5]]), config, space, date(2026, 10, 10))
    assert scheduled[-1]['context']['scheduled_date'] == '2026-10-12'
    with pytest.raises(DomainConflict) as exc:
        schedule_tasks(deepcopy(scheduled), config, {**space, 'target_date': '2026-10-11'}, date(2026, 10, 10))
    assert 'target_date' in exc.value.details['conflicts']


def test_http_preview_auth_strict_contract_and_no_idempotency_key():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    from knowpath_backend.learning.config import LearningSettings
    app = create_app(settings=LearningSettings(local_token="order-test-token"))
    state, space_id, _ = seed(InMemoryMaterialRepository())
    app.state.learning_state = state
    plan = state.create_plan(space_id, {})
    before = deepcopy(state.plan_sessions.repository.materials.assessment_data)
    payload = {"expected_plan_version": 1, "order_mode": "custom", "ordered_node_ids": [t["node_id"] for t in reversed(plan["tasks"])]}
    url = f"/api/v1/plans/{plan['id']}"
    with TestClient(app) as client:
        assert client.post(url + "/order-preview", json=payload).status_code == 401
        headers = {"X-Local-Token": "order-test-token"}
        preview = client.post(url + "/order-preview", json=payload, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["can_apply"] is True
        assert state.plan_sessions.repository.materials.assessment_data == before
        assert client.post(url + "/order-preview", json={**payload, "unknown": True}, headers=headers).status_code == 422
        applied = client.patch(url + "/order", json=payload, headers={**headers, "Idempotency-Key": "http-order"})
        assert applied.status_code == 200
        assert applied.json()["version"] == 2
        assert client.patch(url + "/order", json=payload, headers={**headers, "Idempotency-Key": "http-order"}).json() == applied.json()


def test_source_adjusts_movable_tasks_before_a_fixed_active_dependent():
    from knowpath_backend.learning.plans.order_commands import fill_ordered_slots
    def task(identifier, prerequisites=()):
        return {'id': identifier, 'node_id': identifier, 'topic_ids': [identifier], 'status': 'pending',
                'context': {'prerequisites': list(prerequisites)}}
    parent, unrelated, active = task('parent'), task('unrelated'), task('active', ['parent'])
    result = fill_ordered_slots([parent, active, unrelated], [unrelated, parent], {'active'}, {})
    assert [t['id'] for t in result] == ['parent', 'active', 'unrelated']


@pytest.mark.parametrize("count, constraint", [(3, "session_count"), (5, "target_date")])
def test_apply_schedule_conflict_rolls_back_plan_and_replay(ordered_backend, monkeypatch, count, constraint):
    from dataclasses import replace
    from datetime import date, timedelta
    state, space_id, _ = seed(ordered_backend, legacy=True)
    state.update_profile(space_id, {"target_date": (date.today() + timedelta(days=2)).isoformat(), "expected_version": 1})
    topics = state.plan_sessions._ordered_topics(state.space_service.repository.get(space_id), {})
    states = {t["id"]: {"status": "unstable"} for t in topics[2:]}
    monkeypatch.setattr(state.plan_sessions, "_planning_states", lambda *args, **kwargs: deepcopy(states))
    state.plan_sessions.policy = replace(state.plan_sessions.policy, learn_minutes=20)
    plan = state.create_plan(space_id, {"session_count": count})
    assert [t["estimated_minutes"] for t in plan["tasks"]] == [20, 20, 15, 15]
    payload = {"expected_plan_version": 1, "order_mode": "custom", "ordered_node_ids": [plan["tasks"][i]["node_id"] for i in [0, 2, 1, 3]]}
    preview = state.preview_plan_order(plan["id"], payload)
    assert preview["can_apply"] is False
    assert constraint in preview["conflicts"][0]["conflicts"]
    with pytest.raises(DomainConflict):
        state.update_plan_order(plan["id"], payload, idempotency_key="rejected-order")
    assert state.get_plan(plan["id"]) == plan
    corrected = {**payload, "ordered_node_ids": [t["node_id"] for t in plan["tasks"]]}
    assert state.update_plan_order(plan["id"], corrected, idempotency_key="rejected-order")["version"] == 2


def test_sql_preview_leaves_every_table_unchanged(tmp_path):
    from sqlalchemy import select
    from knowpath_backend.learning.persistence.db import Base
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'readonly.db'}")
    init_db(engine)
    state, space_id, _ = seed(SqlAlchemyMaterialRepository(engine))
    plan = state.create_plan(space_id, {})
    def snapshot():
        with engine.connect() as connection:
            return {name: [tuple(row) for row in connection.execute(select(table))] for name, table in Base.metadata.tables.items()}
    before = snapshot()
    state.preview_plan_order(plan["id"], {"expected_plan_version": 1, "order_mode": "source"})
    assert snapshot() == before
    engine.dispose()

def test_completed_schedule_from_earlier_plan_does_not_reserve_future_capacity():
    from datetime import date
    from knowpath_backend.learning.plans.policy import schedule_tasks
    completed = {'id': 'done', 'status': 'completed', 'estimated_minutes': 30,
                 'context': {'position': 0, 'session_index': 0, 'scheduled_date': '2026-10-09'}}
    future = {'id': 'future', 'status': 'pending', 'estimated_minutes': 10, 'context': {}}
    scheduled = schedule_tasks(deepcopy([completed, future]), {'session_count': 3, 'minutes_per_session': 30},
                               {'weekly_minutes': 30}, date(2026, 10, 10), fixed_task_ids={'done'})
    assert scheduled[0]['context'] == {**completed['context'], 'node_id': 'done', 'sequence': 1}
    assert scheduled[1]['context']['session_index'] == 0


def test_active_session_from_base_plan_fixes_inherited_node(ordered_backend):
    state, space_id, _ = seed(ordered_backend)
    plan = state.create_plan(space_id, {})
    active = state.start_session(plan['id'], plan['tasks'][0]['id'])
    rebuilt = state.create_plan(space_id, {'rebuild_mode': 'local_replan', 'base_plan_id': plan['id'], 'expected_plan_version': 1})
    preview = state.preview_plan_order(rebuilt['id'], {'expected_plan_version': 1, 'order_mode': 'source'})
    assert preview['fixed_nodes'][0]['node_id'] == plan['tasks'][0]['node_id']
    assert preview['fixed_nodes'][0]['reason'] == 'active_session'
    assert state.get_workbench(space_id)['active_session']['id'] == active['id']

def test_custom_preference_appends_new_and_excludes_removed_topics(ordered_backend):
    state, space_id, topics = seed(ordered_backend, legacy=True)
    functions = [t['id'] for t in topics if t['name'] == 'Functions']
    loops = [t['id'] for t in topics if t['name'] == 'Loops']
    state.set_scope(space_id, {'topic_ids': functions, 'expected_version': 2})
    first = state.create_plan(space_id, {})
    preferred = state.update_plan_order(first['id'], {'expected_plan_version': 1, 'order_mode': 'custom',
                   'ordered_node_ids': [t['node_id'] for t in reversed(first['tasks'])]})
    state.set_scope(space_id, {'topic_ids': functions + loops, 'expected_version': 3})
    expanded = state.create_plan(space_id, {'rebuild_mode': 'local_replan', 'base_plan_id': first['id'], 'expected_plan_version': preferred['version']})
    assert [t['context']['title'] for t in expanded['tasks']] == ['Functions', 'Functions', 'Loops', 'Loops']
    loop_canonical = next(t['canonical_topic_id'] for t in topics if t['name'] == 'Loops')
    assert expanded['config']['custom_topic_order'][-1] == loop_canonical
    state.set_scope(space_id, {'topic_ids': loops, 'expected_version': 4})
    reduced = state.create_plan(space_id, {'rebuild_mode': 'local_replan', 'base_plan_id': expanded['id'], 'expected_plan_version': 1})
    assert reduced['config']['custom_topic_order'] == [loop_canonical]

def test_label_uses_earliest_known_source_ref():
    from knowpath_backend.learning.plans.ordering import source_key, source_label
    topic = {'id': 'many', 'name': 'Many', 'source_refs': [{'material_id': 'm', 'line_start': 20}, {'material_id': 'm', 'line_start': 2}]}
    assert source_label(topic) == '第 2 行'
    unknown_early_material = {'id': 'many', 'name': 'Many', 'source_refs': [{'material_id': 'first'}, {'material_id': 'second', 'line_start': 2}]}
    assert source_key(unknown_early_material, ['first', 'second'])[1] == 1

def test_custom_conflict_has_legal_readonly_suggestion(ordered_backend, monkeypatch):
    from sqlalchemy import select
    from knowpath_backend.learning.persistence.db import Base
    state, space_id, _ = seed(ordered_backend, legacy=True)
    bound = state.space_service.bound_topics
    def dependencies(space):
        topics = bound(space)
        topics[-1]['prerequisites'] = [topics[0]['id']]
        return topics
    monkeypatch.setattr(state.space_service, 'bound_topics', dependencies)
    plan = state.create_plan(space_id, {})
    payload = {'expected_plan_version': 1, 'order_mode': 'custom', 'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]}
    def snapshot():
        if isinstance(ordered_backend, InMemoryMaterialRepository):
            return deepcopy(ordered_backend.assessment_data)
        with ordered_backend.unit_of_work.engine.connect() as connection:
            return {name: [tuple(row) for row in connection.execute(select(table))] for name, table in Base.metadata.tables.items()}
    before = snapshot()
    preview = state.preview_plan_order(plan['id'], payload)
    assert not preview['can_apply']
    assert preview['order_mode'] == 'custom'
    assert preview['material_order'] == plan['config']['material_order']
    assert [t['node_id'] for t in preview['tasks']] == payload['ordered_node_ids']
    conflict = preview['conflicts'][0]
    assert conflict['code'] == 'ORDER_PREREQUISITE_CONFLICT'
    assert conflict['node_label'] in conflict['message']
    assert conflict['prerequisite_label'] in conflict['message']
    suggestion = preview['suggested_order']
    assert suggestion['expected_plan_version'] == 1
    assert set(suggestion['ordered_node_ids']) == set(payload['ordered_node_ids'])
    assert state.preview_plan_order(plan['id'], suggestion)['can_apply']
    assert snapshot() == before
    with pytest.raises(DomainConflict) as exc:
        state.update_plan_order(plan['id'], payload)
    assert exc.value.details['suggested_order'] == suggestion
    assert snapshot() == before
    applied = state.update_plan_order(plan['id'], suggestion)
    assert [t['node_id'] for t in applied['tasks']] == suggestion['ordered_node_ids']


def test_deferred_prerequisite_explains_no_feasible_suggestion(ordered_backend, monkeypatch):
    state, space_id, _ = seed(ordered_backend, legacy=True)
    bound = state.space_service.bound_topics
    def dependencies(space):
        topics = bound(space)
        topics[-1]['prerequisites'] = [topics[0]['id']]
        return topics
    monkeypatch.setattr(state.space_service, 'bound_topics', dependencies)
    plan = state.create_plan(space_id, {})
    state.update_task(plan['id'], plan['tasks'][0]['id'], {'expected_plan_version': 1, 'status': 'deferred', 'defer_until': '2099-01-01T00:00:00+00:00'})
    before = state.get_plan(plan['id'])
    payload = {'expected_plan_version': 2, 'order_mode': 'custom', 'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]}
    preview = state.preview_plan_order(plan['id'], payload)
    assert not preview['can_apply']
    assert preview['suggested_order'] is None
    assert preview['suggestion_unavailable_reason']
    assert preview['suggestion_conflicts'][0]['reason'] == 'deferred_prerequisite'
    assert state.get_plan(plan['id']) == before


@pytest.mark.parametrize('ref', [{'page': True}, {'line_start': False}, {'page': 0}, {'line_start': -1}, {'page': '3'}, {'section': ''}, {'section': 'arbitrary'}, {'chunk_id': 'missing'}, {'source_unit_id': 'missing'}])
def test_unverified_locator_has_exact_unknown_label(ref):
    from knowpath_backend.learning.plans.ordering import source_key, source_label
    topic = {'id': 'unknown', 'name': 'Unknown', 'source_refs': [{'material_id': 'm', **ref}]}
    assert source_label(topic) == '原文位置未记录'
    assert source_key(topic, ['m'])[0] == 1


def test_snapshot_locator_label_requires_matching_bound_version():
    from knowpath_backend.learning.plans.ordering import source_label
    topic = {'id': 'known', 'source_refs': [{'material_id': 'm', 'material_version_id': 'v', 'chunk_id': 'chunk'}]}
    positions = {('m', 'v', 'chunk'): 0}
    assert source_label(topic, ['m'], positions) == '来源片段顺序'
    topic['source_refs'][0]['material_version_id'] = 'old-v'
    assert source_label(topic, ['m'], positions) == '原文位置未记录'

def test_custom_node_interleaving_survives_unchanged_local_replan(ordered_backend):
    state, space_id, _ = seed(ordered_backend, legacy=True)
    plan = state.create_plan(space_id, {})
    expected = [plan['tasks'][i]['node_id'] for i in [0, 2, 1, 3]]
    ordered = state.update_plan_order(plan['id'], {'expected_plan_version': 1, 'order_mode': 'custom', 'ordered_node_ids': expected})
    rebuilt = state.create_plan(space_id, {'rebuild_mode': 'local_replan', 'base_plan_id': plan['id'], 'expected_plan_version': ordered['version']})
    assert [t['node_id'] for t in rebuilt['tasks']] == expected
    assert rebuilt['config']['custom_node_order'] == expected

def test_suggestion_checks_source_schedule_capacity(ordered_backend, monkeypatch):
    from dataclasses import replace
    state, space_id, _ = seed(ordered_backend, legacy=True)
    bound = state.space_service.bound_topics
    # Keep source locators distinct so source task durations alternate 20/15.
    original = state.plan_sessions._ordered_topics(state.space_service.repository.get(space_id), {})
    source_ids = [t['id'] for t in original]
    def dependencies(space):
        topics = bound(space)
        for t in topics:
            i = source_ids.index(t['id'])
            t['source_refs'][0]['line_start'] = i + 1
            if i == 2:
                t['prerequisites'] = [source_ids[0]]
        return topics
    monkeypatch.setattr(state.space_service, 'bound_topics', dependencies)
    states = {source_ids[i]: {'status': 'unstable'} for i in [1, 3]}
    monkeypatch.setattr(state.plan_sessions, '_planning_states', lambda *args, **kwargs: deepcopy(states))
    state.plan_sessions.policy = replace(state.plan_sessions.policy, learn_minutes=20)
    plan = state.create_plan(space_id, {'order_mode': 'adaptive', 'schedule_mode': 'session_window'})
    assert [t['estimated_minutes'] for t in plan['tasks']] == [15, 15, 20, 20]
    preview = state.preview_plan_order(plan['id'], {'expected_plan_version': 1, 'order_mode': 'custom', 'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]})
    assert not preview['can_apply']
    assert preview['suggested_order'] is None
    assert 'session_count' in preview['suggestion_conflicts'][0]['conflicts']
    assert state.get_plan(plan['id']) == plan
