"""Deleted model-job owners leave neither runnable jobs nor late publications."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from knowpath_backend.learning.errors import DomainNotFound
from knowpath_backend.learning.persistence.learning_repository import TABLES
from knowpath_backend.learning.material_deletion import references
from knowpath_backend.test.test_learning_material_deletion import workspace, seed, service
from knowpath_backend.test.test_learning_model_tasks import enqueue, worker
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions, FixedAnswer


def delete_space(state, identifier):
    return state.delete_space(identifier, {'confirm': True, 'expected_version': state.get_space(identifier)['space_version']})


def delete_material(state, identifier):
    return service(state).delete(identifier, {'confirm': True, 'cascade': True, 'expected_version': 1})


@pytest.mark.parametrize('kind', ['assessment', 'message'])
@pytest.mark.parametrize('claimed', [False, True])
def test_space_delete_removes_owned_outbox_and_keeps_other_spaces(workspace, kind, claimed):
    state = workspace()
    first, _, space = seed(state)
    other = state.create_space({'name': 'Keep', 'material_ids': [first.id]})
    response, event = enqueue(state, space['id'], kind)
    other_response, _ = enqueue(state, other['id'], kind, key='other-job')
    repo = state.assessment_service.repository
    keep = next(row for row in repo.records('outbox', event_type=kind + '.generate') if row['payload']['run_id'] == other_response['run_id'])
    if claimed:
        assert worker(state, datetime.now(timezone.utc)).claim(event['id'])
    # Knowledge update history is also owned by its space.
    history = dict(event, id=str(uuid4()), event_type='knowledge.updated', aggregate_type='learning_space',
                   aggregate_id=space['id'], status='completed', payload={'space_id': space['id'], 'run_id': response['run_id']})
    repo.put_record('outbox', history)
    delete_space(state, space['id'])
    for identifier in (event['id'], history['id']):
        with pytest.raises(DomainNotFound):
            repo.get_record('outbox', identifier)
    with pytest.raises(DomainNotFound):
        state.get_run(response['run_id'])
    assert repo.get_record('outbox', keep['id'])['status'] == 'pending'
    assert state.get_run(other_response['run_id'])['status'] == 'queued'


@pytest.mark.parametrize('kind', ['assessment', 'message'])
@pytest.mark.parametrize('claimed', [False, True])
def test_material_delete_cancels_generation_and_revokes_lease(workspace, kind, claimed):
    state = workspace()
    first, second, space = seed(state)
    other = state.create_space({'name': 'Keep', 'material_ids': [second.id]})
    response, event = enqueue(state, space['id'], kind)
    other_response, _ = enqueue(state, other['id'], kind, key='keep-job')
    if claimed:
        assert worker(state, datetime.now(timezone.utc)).claim(event['id'])
    delete_material(state, first.id)
    repo = state.assessment_service.repository
    cancelled = repo.get_record('outbox', event['id'])
    assert cancelled['status'] == 'cancelled'
    assert cancelled['lease_token'] is None and cancelled['lease_until'] is None and cancelled['available_at'] is None
    assert state.get_run(response['run_id'])['status'] == 'cancelled'
    resource = repo.get_record('assessments' if kind == 'assessment' else 'messages', event['aggregate_id'])
    assert not references(resource['snapshot'], first.id)
    assert state.get_run(other_response['run_id'])['status'] == 'queued'


@pytest.mark.parametrize('kind', ['assessment', 'message'])
@pytest.mark.parametrize('owner', ['space', 'material'])
def test_deletion_during_provider_call_blocks_late_publication(workspace, kind, owner):
    state = workspace()
    first, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    class DeleteDuringGeneration:
        def generate(self, *args):
            if owner == 'space':
                delete_space(workspace(), space['id'])
            else:
                delete_material(workspace(), first.id)
            return (FixedQuestions() if kind == 'assessment' else FixedAnswer()).generate(*args)
    state.assessment_service.generator = state.message_service.generator = DeleteDuringGeneration()
    worker(state, datetime.now(timezone.utc)).run_once(event['id'])
    repo = state.assessment_service.repository
    if owner == 'space':
        with pytest.raises(DomainNotFound):
            repo.get_record('outbox', event['id'])
        with pytest.raises(DomainNotFound):
            state.get_run(response['run_id'])
    else:
        assert repo.get_record('outbox', event['id'])['status'] == 'cancelled'
        run = state.get_run(response['run_id'])
        assert run['status'] == 'cancelled'
        assert not [row for row in run['events'] if row['event'] in {'message.delta', 'message.completed', 'run.completed'}]


@pytest.mark.parametrize('kind', ['assessment', 'message'])
@pytest.mark.parametrize('missing', ['resource', 'space', 'run'])
def test_orphaned_model_event_is_settled_without_calling_provider(workspace, kind, missing):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    repo = state.assessment_service.repository
    consumer = worker(state, datetime.now(timezone.utc))
    claimed = consumer.claim(event['id'])
    class MustNotCall:
        def generate(self, *args):
            pytest.fail('orphaned jobs must not call providers')
    state.assessment_service.generator = state.message_service.generator = MustNotCall()
    with repo.transaction():
        if missing == 'space':
            state.space_service.repository.delete(space['id'])
        elif missing == 'resource':
            table = 'assessments' if kind == 'assessment' else 'messages'
            if hasattr(repo, 'unit_of_work'):
                with repo.unit_of_work.session() as session:
                    session.execute(delete(TABLES[table]).where(TABLES[table].id == event['aggregate_id']))
            else:
                state.material_repository.assessment_data[table].pop(event['aggregate_id'])
        else:
            if hasattr(repo, 'unit_of_work'):
                from knowpath_backend.learning.persistence.db import RunRow, RunEventRow
                with repo.unit_of_work.session() as session:
                    session.execute(delete(RunEventRow).where(RunEventRow.run_id == response['run_id']))
                    session.execute(delete(RunRow).where(RunRow.id == response['run_id']))
            else:
                state.run_service.repository._runs.pop(response['run_id'])
    consumer.execute(claimed)
    cancelled = repo.get_record('outbox', event['id'])
    assert cancelled['status'] == 'cancelled'
    assert cancelled['lease_token'] is None and cancelled['lease_until'] is None
    assert consumer.claim(event['id']) is None
    if missing != 'run':
        assert state.get_run(response['run_id'])['status'] == 'cancelled'


@pytest.mark.parametrize('kind', ['assessment', 'message'])
@pytest.mark.parametrize('owner', ['space', 'material'])
def test_delete_failure_rolls_back_model_job_cancellation(workspace, kind, owner, monkeypatch):
    state = workspace()
    first, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    claimed = worker(state, datetime.now(timezone.utc)).claim(event['id'])
    original = state.run_service.create
    def fail_deletion(run_kind, *args, **kwargs):
        if run_kind in {'space_delete', 'material_delete'}:
            raise RuntimeError('delete rollback')
        return original(run_kind, *args, **kwargs)
    monkeypatch.setattr(state.run_service, 'create', fail_deletion)
    with pytest.raises(RuntimeError, match='delete rollback'):
        delete_space(state, space['id']) if owner == 'space' else delete_material(state, first.id)
    assert state.assessment_service.repository.get_record('outbox', event['id']) == claimed
    assert state.get_run(response['run_id'])['status'] == 'running'


@pytest.mark.parametrize('fails', [False, True])
def test_model_worker_cli_closes_retriever_and_engine(monkeypatch, fails):
    from knowpath_backend.learning import model_worker_cli as cli
    calls = []
    engine = SimpleNamespace(dispose=lambda: calls.append('engine'))
    retriever = SimpleNamespace(close=lambda: calls.append('retriever'))
    state = SimpleNamespace(assessment_service=object(), message_service=SimpleNamespace(retriever=retriever))
    class Consumer:
        def __init__(self, *args, **kwargs):
            pass
        def run_once(self):
            if fails:
                raise RuntimeError('storage failure')
            return False
    monkeypatch.setattr(cli, 'load_dotenv', lambda: None)
    monkeypatch.setattr(cli, 'create_db_engine', lambda: engine)
    monkeypatch.setattr(cli, 'SqlAlchemyMaterialRepository', lambda value: value)
    monkeypatch.setattr(cli, 'LearningState', lambda value: state)
    monkeypatch.setattr(cli, 'ModelTaskWorker', Consumer)
    assert cli.main(['--once']) == (1 if fails else 0)
    assert calls == ['retriever', 'engine']


def test_material_delete_cancels_message_from_previous_space_binding(workspace):
    state = workspace()
    first, _, space = seed(state)
    response, event = enqueue(state, space['id'], 'message')
    # The queued snapshot still owns its original sources after a scope change.
    repo = state.assessment_service.repository
    with repo.transaction():
        current = state.space_service.repository.get(space['id'])
        current['bindings'] = [row for row in current['bindings'] if row['material_id'] != first.id]
        current['scope_version'] += 1
        state.space_service.repository.put(current)
    delete_material(state, first.id)
    assert repo.get_record('outbox', event['id'])['status'] == 'cancelled'
    assert state.get_run(response['run_id'])['status'] == 'cancelled'
    assert not references(repo.get_record('messages', event['aggregate_id'])['snapshot'], first.id)
