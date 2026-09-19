"""Durable model tasks retain the original Run across retry and worker restart."""
from datetime import datetime, timedelta, timezone
import pytest
from knowpath_backend.test.test_learning_material_deletion import workspace, seed
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions, FixedAnswer
from knowpath_backend.learning.assessments.generation import QuestionGenerationError
from knowpath_backend.learning.conversations.generation import MessageGenerationError
from knowpath_backend.learning.rag.retrieval import KeywordRetriever

PAYLOAD = {'kind': 'diagnostic', 'question_count': 5, 'question_types': ['single_choice'],
           'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0}}


def enqueue(state, space_id, kind, key='generation'):
    if kind == 'assessment':
        result = state.assessment_service.create(space_id, PAYLOAD, key, durable=True)
    else:
        result = state.message_service.send(space_id, {'message': 'Explain functions'}, key, durable=True)
    event = state.assessment_service.repository.records('outbox', event_type=f'{kind}.generate')[0]
    return result, event


def worker(state, timestamp, **kwargs):
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    state.message_service.retriever = KeywordRetriever()
    return ModelTaskWorker(state.assessment_service, state.message_service, clock=lambda: timestamp, **kwargs)


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_atomic_enqueue_and_idempotent_replay_never_call_model(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    class MustNotCall:
        def generate(self, *args):
            pytest.fail('enqueue must not call the model')
    state.assessment_service.generator = state.message_service.generator = MustNotCall()
    response, event = enqueue(state, space['id'], kind)
    replay, again = enqueue(state, space['id'], kind)
    assert replay == response and event['id'] == again['id']
    assert event['payload']['run_id'] == response['run_id']
    assert event['status'] == 'pending'
    assert state.get_run(response['run_id'])['status'] == 'queued'
    assert len(state.assessment_service.repository.records('outbox', event_type=f'{kind}.generate')) == 1


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_transient_failure_retries_same_run_after_restart(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    class Unavailable:
        def generate(self, *args):
            raise (QuestionGenerationError('MODEL_UNAVAILABLE') if kind == 'assessment' else MessageGenerationError('MODEL_UNAVAILABLE'))
    state.assessment_service.generator = state.message_service.generator = Unavailable()
    timestamp = datetime.now(timezone.utc)
    job = worker(state, timestamp)
    assert job.run_once(event['id'])
    pending = state.assessment_service.repository.get_record('outbox', event['id'])
    assert pending['status'] == 'pending' and pending['attempts'] == 1
    assert pending['payload']['last_error']['code'] == 'MODEL_UNAVAILABLE'
    assert state.get_run(response['run_id'])['status'] == 'running'
    assert job.run_once(event['id']) is False
    restored = workspace()
    restored.assessment_service.generator = FixedQuestions()
    restored.message_service.generator = FixedAnswer()
    assert worker(restored, timestamp + timedelta(seconds=10)).run_once(event['id'])
    assert restored.get_run(response['run_id'])['status'] == 'succeeded'
    assert restored.assessment_service.repository.get_record('outbox', event['id'])['status'] == 'completed'


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_retry_limit_and_error_classification(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    class RateLimited:
        def generate(self, *args):
            raise (QuestionGenerationError('RATE_LIMITED') if kind == 'assessment' else MessageGenerationError('RATE_LIMITED'))
    state.assessment_service.generator = state.message_service.generator = RateLimited()
    timestamp = datetime.now(timezone.utc)
    assert worker(state, timestamp, max_attempts=2).run_once(event['id'])
    assert worker(state, timestamp + timedelta(seconds=10), max_attempts=2).run_once(event['id'])
    run = workspace().get_run(response['run_id'])
    assert run['status'] == 'failed' and run['error']['code'] == 'RATE_LIMITED'
    assert workspace().assessment_service.repository.get_record('outbox', event['id'])['status'] == 'failed'


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_unsupported_model_is_terminal_not_reclassified(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    class Unsupported:
        def generate(self, *args):
            raise (QuestionGenerationError('UNSUPPORTED_MODEL') if kind == 'assessment' else MessageGenerationError('UNSUPPORTED_MODEL'))
    state.assessment_service.generator = state.message_service.generator = Unsupported()
    worker(state, datetime.now(timezone.utc)).run_once(event['id'])
    run = state.get_run(response['run_id'])
    assert run['status'] == 'failed' and run['error']['code'] == 'UNSUPPORTED_MODEL'
    assert state.assessment_service.repository.get_record('outbox', event['id'])['attempts'] == 1


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_lease_takeover_discards_the_previous_workers_late_result(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    timestamp = datetime.now(timezone.utc)
    first_worker = worker(state, timestamp, lease_seconds=2)
    claimed = first_worker.claim(event['id'])
    assert claimed
    assert worker(workspace(), timestamp, lease_seconds=2).claim(event['id']) is None
    restored = workspace()
    restored.assessment_service.generator = FixedQuestions()
    restored.message_service.generator = FixedAnswer()
    second_worker = worker(restored, timestamp + timedelta(seconds=3), lease_seconds=2)
    class LateResult:
        def generate(self, *args):
            assert second_worker.run_once(event['id'])
            return (FixedQuestions() if kind == 'assessment' else FixedAnswer()).generate(*args)
    state.assessment_service.generator = state.message_service.generator = LateResult()
    first_worker.execute(claimed)
    run = state.get_run(response['run_id'])
    assert run['status'] == 'succeeded'
    assert len([e for e in run['events'] if e['event'] == 'run.completed']) == 1
    if kind == 'message':
        assert len([e for e in run['events'] if e['event'] == 'message.completed']) == 1


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_enqueue_failure_rolls_back_resource(workspace, kind, monkeypatch):
    state = workspace()
    _, _, space = seed(state)
    repo = state.assessment_service.repository
    original = repo.put_record
    def fail(table, row):
        if table == 'outbox':
            raise RuntimeError('outbox unavailable')
        return original(table, row)
    monkeypatch.setattr(repo, 'put_record', fail)
    with pytest.raises(RuntimeError, match='outbox unavailable'):
        enqueue(state, space['id'], kind)
    assert workspace().assessment_service.repository.records('assessments' if kind == 'assessment' else 'messages') == []


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_cancel_before_claim_settles_without_provider(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    class MustNotCall:
        def generate(self, *args):
            pytest.fail('cancelled jobs must not call the model')
    state.assessment_service.generator = state.message_service.generator = MustNotCall()
    state.run_service.request_cancel(response['run_id'])
    assert worker(state, datetime.now(timezone.utc)).run_once(event['id']) is False
    assert state.get_run(response['run_id'])['status'] == 'cancelled'
    assert state.assessment_service.repository.get_record('outbox', event['id'])['status'] == 'cancelled'


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_crashed_final_attempt_is_terminal_after_lease_expiry(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    timestamp = datetime.now(timezone.utc)
    assert worker(state, timestamp, max_attempts=1, lease_seconds=2).claim(event['id'])
    restored = workspace()
    assert worker(restored, timestamp + timedelta(seconds=3), max_attempts=1, lease_seconds=2).run_once(event['id']) is False
    assert restored.get_run(response['run_id'])['status'] == 'failed'
    assert restored.assessment_service.repository.get_record('outbox', event['id'])['attempts'] == 1


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_unknown_provider_error_is_sanitized_and_retryable(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    class Broken:
        def generate(self, *args):
            raise RuntimeError('secret-api-key-do-not-expose')
    state.assessment_service.generator = state.message_service.generator = Broken()
    worker(state, datetime.now(timezone.utc)).run_once(event['id'])
    pending = state.assessment_service.repository.get_record('outbox', event['id'])
    assert pending['status'] == 'pending'
    assert pending['payload']['last_error']['code'] == 'MODEL_UNAVAILABLE'
    assert 'secret-api-key' not in repr(pending) + repr(state.get_run(response['run_id']))


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_publication_storage_failure_rolls_back_and_reclaims(workspace, kind, monkeypatch):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    state.assessment_service.generator, state.message_service.generator = FixedQuestions(), FixedAnswer()
    repo = state.assessment_service.repository
    original = repo.put_record
    def fail_completed(table, row):
        if table == 'outbox' and row['status'] == 'completed':
            raise RuntimeError('storage unavailable')
        return original(table, row)
    monkeypatch.setattr(repo, 'put_record', fail_completed)
    timestamp = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match='storage unavailable'):
        worker(state, timestamp, lease_seconds=2).run_once(event['id'])
    assert state.get_run(response['run_id'])['status'] == 'running'
    assert not [e for e in state.get_run(response['run_id'])['events'] if e['event'] in {'run.completed', 'message.completed', 'message.delta'}]
    monkeypatch.setattr(repo, 'put_record', original)
    restored = workspace()
    restored.assessment_service.generator, restored.message_service.generator = FixedQuestions(), FixedAnswer()
    worker(restored, timestamp + timedelta(seconds=3), lease_seconds=2).run_once(event['id'])
    assert restored.get_run(response['run_id'])['status'] == 'succeeded'


def test_embedding_unavailable_retries_without_answer_model(workspace):
    from knowpath_backend.learning.rag.retrieval import RetrievalError
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], 'message')
    class NoEmbeddings:
        def select(self, *args, **kwargs):
            raise RetrievalError('EMBEDDING_UNAVAILABLE')
    class MustNotCall:
        def generate(self, *args):
            pytest.fail('answer model requires successful retrieval')
    timestamp = datetime.now(timezone.utc)
    consumer = worker(state, timestamp)
    state.message_service.retriever = NoEmbeddings()
    state.message_service.generator = MustNotCall()
    consumer.run_once(event['id'])
    pending = state.assessment_service.repository.get_record('outbox', event['id'])
    assert pending['status'] == 'pending'
    assert pending['payload']['last_error']['code'] == 'EMBEDDING_UNAVAILABLE'
    assert state.get_run(response['run_id'])['status'] == 'running'


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_simultaneous_workers_only_one_claims(workspace, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    timestamp = datetime.now(timezone.utc)
    barrier = Barrier(2)
    consumers = [worker(workspace(), timestamp), worker(workspace(), timestamp)]
    def claim(consumer):
        barrier.wait(timeout=10)
        return consumer.claim(event['id'])
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, consumers))
    assert sum(result is not None for result in results) == 1
    assert state.assessment_service.repository.get_record('outbox', event['id'])['attempts'] == 1


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_cancellation_during_model_call_discards_response(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    class CancelWhileGenerating:
        def generate(self, *args):
            state.run_service.request_cancel(response['run_id'])
            return (FixedQuestions() if kind == 'assessment' else FixedAnswer()).generate(*args)
    state.assessment_service.generator = state.message_service.generator = CancelWhileGenerating()
    worker(state, datetime.now(timezone.utc)).run_once(event['id'])
    run = state.get_run(response['run_id'])
    assert run['status'] == 'cancelled'
    assert state.assessment_service.repository.get_record('outbox', event['id'])['status'] == 'cancelled'
    assert not [e for e in run['events'] if e['event'] in {'run.completed', 'message.completed', 'message.delta'}]


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_optional_dispatch_only_nudges_committed_outbox_once(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    calls = []
    dispatch = lambda function, identifier: calls.append((function, identifier))
    if kind == 'assessment':
        create = lambda: state.assessment_service.create(space['id'], PAYLOAD, 'nudge', durable=True, dispatch=dispatch)
    else:
        create = lambda: state.message_service.send(space['id'], {'message': 'Explain functions'}, 'nudge', durable=True, dispatch=dispatch)
    first = create()
    assert create() == first
    assert len(calls) == 1
    function, identifier = calls[0]
    event = state.assessment_service.repository.get_record('outbox', identifier)
    assert event['payload']['run_id'] == first['run_id']
    state.assessment_service.generator, state.message_service.generator = FixedQuestions(), FixedAnswer()
    state.message_service.retriever = KeywordRetriever()
    assert function(identifier)
    assert state.get_run(first['run_id'])['status'] == 'succeeded'
