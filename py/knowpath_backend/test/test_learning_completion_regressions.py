"""Late contract audit regressions: hints, startup recovery and adoption history."""
import pytest
from knowpath_backend.test.test_learning_material_deletion import workspace, seed
from knowpath_backend.test.test_learning_state_persistence import create, answer, FixedAnswer
from knowpath_backend.test.test_learning_knowledge_updates import updates_workspace, changed
from knowpath_backend.test.test_learning_graph_reconciliation import graph_workspace


def test_topic_hint_excludes_all_related_active_questions(workspace):
    state = workspace()
    _, _, space = seed(state)
    assessment = create(state, space['id'])
    plan = state.create_plan(space['id'], {})
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    topic = state.assessment_service.repository.get_record('assessments', assessment['id'])['questions'][0]['topic_id']
    state.add_session_event(session['session_id'], {'type': 'request_hint', 'topic_id': topic})
    answer(state, assessment, finalize=True)
    evidence = state.assessment_service.repository.records('evidence', space_id=space['id'])
    assert evidence and all(not item['eligible'] for item in evidence)


def test_recovery_preserves_durable_but_fails_legacy_model_jobs(workspace):
    from knowpath_backend.learning.startup import recover_legacy_runs
    state = workspace()
    _, _, space = seed(state)
    state.message_service.generator = FixedAnswer()
    old = state.message_service.send(space['id'], {'message':'Old pending'}, 'old', dispatch=lambda *args: None)
    durable = state.assessment_service.create(space['id'], {'question_count':5}, 'durable', durable=True)
    assert recover_legacy_runs(state.run_service, state.assessment_service.repository) == 1
    assert state.get_run(old['run_id'])['error']['code'] == 'RUN_INTERRUPTED'
    assert state.get_run(durable['run_id'])['status'] == 'queued'


def test_adoption_has_immutable_timeline_event(updates_workspace):
    factory, _, label, space = updates_workspace
    state = factory()
    plan = state.create_plan(space['id'], {})
    payload = changed(updates_workspace)
    before = state.changes_for(space['id'])
    result = state.apply_knowledge_updates(space['id'], payload, idempotency_key=label+'-adopt-history')
    after = factory().changes_for(space['id'])
    added = [event for event in after if event['id'] not in {item['id'] for item in before}]
    assert len(added) == 1
    event = added[0]
    assert event['kind'] == 'knowledge_updated'
    assert event['space_version'] == 2
    assert event['affected_topic_ids'] == result['affected_topic_ids']
    assert event['invalidated_plan_ids'] == [plan['id']]
    assert event['previous_bindings'][0]['graph_version'] == 1
    assert event['bindings'][0]['graph_version'] == 2
    state.apply_knowledge_updates(space['id'], payload, idempotency_key=label+'-adopt-history')
    assert factory().changes_for(space['id']) == after
