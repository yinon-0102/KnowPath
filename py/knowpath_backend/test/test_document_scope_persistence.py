"""Frozen document scope survives queue persistence and cannot bypass stale guards."""
import pytest

from knowpath_backend.test.test_learning_state_persistence import workspace
from knowpath_backend.test.test_learning_messages import service
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker


def test_selected_scope_survives_worker_restart(workspace):
    factory, space_id, _, _ = workspace
    state, original_generator = service(factory)
    material_id = state.get_space(space_id)['bindings'][0]['material_id']
    jobs = []
    job = state.send_message(space_id, {'message': 'Explain functions', 'material_ids': [material_id]},
        durable=True, dispatch=lambda fn, identifier: jobs.append(identifier))
    restored, generator = service(factory)
    ModelTaskWorker(messages=restored.message_service).run_once(jobs[0])
    assert original_generator.calls == []
    assert len(generator.calls) == 1
    assert generator.calls[0]['request_scope']['material_ids'] == [material_id]
    assert factory().get_run(job['run_id'])['status'] == 'succeeded'


def test_selected_scope_change_stops_queued_task_before_model(workspace):
    factory, space_id, topic_id, _ = workspace
    state, generator = service(factory)
    space = state.get_space(space_id)
    jobs = []
    job = state.send_message(space_id, {'message': 'Explain', 'material_ids': [space['bindings'][0]['material_id']]},
        dispatch=lambda *args: jobs.append(args))
    other = factory()
    other.set_scope(space_id, {'topic_ids': [topic_id], 'expected_version': space['space_version']})
    jobs[0][0](jobs[0][1])
    assert generator.calls == []
    run = factory().get_run(job['run_id'])
    assert run['status'] == 'failed' and run['error']['code'] == 'STALE_LEARNING_CONTEXT'


def test_default_null_and_omitted_selection_share_idempotency(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    first = state.send_message(space_id, {'message': 'Explain'}, idempotency_key='scope-default')
    assert state.send_message(space_id, {'message': 'Explain', 'material_ids': None}, idempotency_key='scope-default') == first
    assert len(generator.calls) == 1
