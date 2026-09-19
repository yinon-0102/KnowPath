"""Model leases renew only while owned and never extend execution indefinitely."""
from datetime import datetime, timedelta, timezone
from threading import Event, current_thread
from time import monotonic

import pytest

from knowpath_backend.learning.model_tasks import ModelTaskWorker, timestamp
from knowpath_backend.test.test_learning_material_deletion import workspace, seed
from knowpath_backend.test.test_learning_model_tasks import enqueue
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions, FixedAnswer
from knowpath_backend.learning.rag.retrieval import KeywordRetriever


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_heartbeat_keeps_slow_generation_owned_beyond_original_lease(workspace, kind, monkeypatch):
    from knowpath_backend.learning.model_tasks import ModelLease
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    wall = [datetime.now(timezone.utc)]
    consumer = ModelTaskWorker(state.assessment_service, state.message_service,
                               clock=lambda: wall[0], lease_seconds=2, heartbeat_interval=0.01)
    state.message_service.retriever = KeywordRetriever()
    repo = state.assessment_service.repository
    renewed = Event()
    original = ModelLease.renew
    def notify(guard):
        result = original(guard)
        renewed.set()
        return result
    monkeypatch.setattr(ModelLease, 'renew', notify)
    class SlowProvider:
        def generate(self, *args):
            for _ in range(3):
                wall[0] += timedelta(seconds=1)
                deadline = monotonic() + 2
                while timestamp(repo.get_record('outbox', event['id'])['lease_until']) < wall[0] + timedelta(seconds=2):
                    assert monotonic() < deadline, 'heartbeat did not renew'
                    renewed.wait(0.05)
                    renewed.clear()
            other = workspace()
            assert ModelTaskWorker(other.assessment_service, other.message_service, clock=lambda: wall[0], lease_seconds=2).claim(event['id']) is None
            return (FixedQuestions() if kind == 'assessment' else FixedAnswer()).generate(*args)
    state.assessment_service.generator = state.message_service.generator = SlowProvider()
    assert consumer.run_once(event['id'])
    assert state.get_run(response['run_id'])['status'] == 'succeeded'
    assert repo.get_record('outbox', event['id'])['attempts'] == 1


@pytest.mark.parametrize('change', ['expired', 'token', 'cancelled', 'deleted'])
def test_heartbeat_never_revives_expired_or_lost_ownership(workspace, change):
    from knowpath_backend.learning.model_tasks import ModelLease
    from knowpath_backend.learning.errors import DomainNotFound
    from knowpath_backend.learning.learning_repository import TABLES
    from sqlalchemy import delete
    state = workspace()
    _, _, space = seed(state)
    _, event = enqueue(state, space['id'], 'assessment')
    wall = [datetime.now(timezone.utc)]
    consumer = ModelTaskWorker(state.assessment_service, state.message_service, clock=lambda: wall[0], lease_seconds=3)
    claimed = consumer.claim(event['id'])
    guard = ModelLease(consumer, claimed)
    repo = state.assessment_service.repository
    with repo.transaction():
        changed = repo.get_record('outbox', event['id'])
        if change == 'expired':
            wall[0] += timedelta(seconds=4)
        elif change == 'token':
            changed['lease_token'] = 'new-owner'
            repo.put_record('outbox', changed)
        elif change == 'cancelled':
            changed.update(status='cancelled', lease_token=None, lease_until=None)
            repo.put_record('outbox', changed)
        elif hasattr(repo, 'unit_of_work'):
            with repo.unit_of_work.session() as session:
                session.execute(delete(TABLES['outbox']).where(TABLES['outbox'].id == event['id']))
        else:
            state.material_repository.assessment_data['outbox'].pop(event['id'])
    assert guard.renew() is False
    if change == 'deleted':
        with pytest.raises(DomainNotFound):
            repo.get_record('outbox', event['id'])
    else:
        assert repo.get_record('outbox', event['id']) == changed


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_total_execution_limit_discards_late_success_and_preserves_safe_error(workspace, kind):
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    wall = datetime.now(timezone.utc)
    elapsed = [0.0]
    consumer = ModelTaskWorker(state.assessment_service, state.message_service, clock=lambda: wall,
                               lease_seconds=3, max_execution_seconds=1, monotonic=lambda: elapsed[0], max_attempts=1)
    state.message_service.retriever = KeywordRetriever()
    class LateSuccess:
        def generate(self, *args):
            elapsed[0] = 2.0
            return (FixedQuestions() if kind == 'assessment' else FixedAnswer()).generate(*args)
    state.assessment_service.generator = state.message_service.generator = LateSuccess()
    consumer.run_once(event['id'])
    run = state.get_run(response['run_id'])
    assert run['status'] == 'running'
    assert not [item for item in run['events'] if item['event'] in {'run.completed', 'message.completed', 'message.delta'}]
    timed_out = state.assessment_service.repository.get_record('outbox', event['id'])
    assert timed_out['payload']['last_error']['code'] == 'MODEL_TASK_TIMEOUT'
    assert timed_out['status'] == 'processing'
    restored = workspace()
    recovery = ModelTaskWorker(restored.assessment_service, restored.message_service,
        clock=lambda: wall + timedelta(seconds=4), max_attempts=1)
    assert recovery.claim(event['id']) is None
    assert restored.get_run(response['run_id'])['error']['code'] == 'MODEL_TASK_TIMEOUT'
    assert restored.get_run(response['run_id'])['status'] == 'failed'


def test_renewal_only_locks_event_and_stops_at_total_budget(workspace, monkeypatch):
    from knowpath_backend.learning.model_tasks import ModelLease
    state = workspace()
    _, _, space = seed(state)
    _, event = enqueue(state, space['id'], 'assessment')
    wall, elapsed = [datetime.now(timezone.utc)], [0.0]
    consumer = ModelTaskWorker(state.assessment_service, state.message_service, clock=lambda: wall[0],
        monotonic=lambda: elapsed[0], lease_seconds=3, max_execution_seconds=5)
    claimed = consumer.claim(event['id'])
    guard = ModelLease(consumer, claimed)
    repo = state.assessment_service.repository
    original = repo.get_record
    def only_event(table, *args, **kwargs):
        assert table == 'outbox'
        return original(table, *args, **kwargs)
    monkeypatch.setattr(repo, 'get_record', only_event)
    from contextlib import contextmanager
    original_transaction, original_run_get = repo.transaction, state.run_service.get
    active = [False]
    @contextmanager
    def tracked_transaction():
        with original_transaction():
            active[0] = True
            try:
                yield
            finally:
                active[0] = False
    def unlocked_run_get(*args):
        assert not active[0], 'renewal must read Run before locking the event'
        return original_run_get(*args)
    monkeypatch.setattr(repo, 'transaction', tracked_transaction)
    monkeypatch.setattr(state.run_service, 'get', unlocked_run_get)
    monkeypatch.setattr(state.space_service.repository, 'get', lambda *args: pytest.fail('renewal must not lock space'))
    wall[0] += timedelta(seconds=1)
    elapsed[0] = 1.0
    assert guard.renew()
    previous = repo.get_record('outbox', event['id'])
    elapsed[0] = 6.0
    assert guard.renew() is False
    ended = repo.get_record('outbox', event['id'])
    assert ended['lease_until'] == previous['lease_until']
    assert ended['payload']['last_error']['code'] == 'MODEL_TASK_TIMEOUT'


def test_heartbeat_shutdown_is_bounded_and_a_blocked_tick_cannot_renew_after_stop(workspace, monkeypatch):
    from knowpath_backend.learning.model_tasks import ModelLease
    state = workspace()
    _, _, space = seed(state)
    _, event = enqueue(state, space['id'], 'assessment')
    wall = [datetime.now(timezone.utc)]
    consumer = ModelTaskWorker(state.assessment_service, state.message_service, clock=lambda: wall[0],
        lease_seconds=3, heartbeat_interval=0.01)
    claimed = consumer.claim(event['id'])
    guard = ModelLease(consumer, claimed)
    repo = state.assessment_service.repository
    original = repo.get_record
    entered, release = Event(), Event()
    def block_heartbeat(table, *args, **kwargs):
        if current_thread().name.startswith('model-lease-'):
            entered.set()
            assert release.wait(3)
        return original(table, *args, **kwargs)
    monkeypatch.setattr(repo, 'get_record', block_heartbeat)
    guard.start()
    try:
        assert entered.wait(2)
        assert guard.thread.daemon
        wall[0] += timedelta(seconds=1)
        started = monotonic()
        guard.close()
        assert monotonic() - started < 0.75
    finally:
        release.set()
        guard.close()
        guard.thread.join(timeout=2)
    assert not guard.thread.is_alive()
    assert repo.get_record('outbox', event['id'])['lease_until'] == claimed['lease_until']


@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf'), 3601])
def test_invalid_execution_budget_is_rejected(workspace, value):
    state = workspace()
    with pytest.raises(ValueError):
        ModelTaskWorker(state.assessment_service, state.message_service, max_execution_seconds=value)


def test_run_cancellation_stops_renewal_without_locking_run_in_event_transaction(workspace):
    from knowpath_backend.learning.model_tasks import ModelLease
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], 'assessment')
    wall = [datetime.now(timezone.utc)]
    consumer = ModelTaskWorker(state.assessment_service, state.message_service, clock=lambda: wall[0], lease_seconds=3)
    claimed = consumer.claim(event['id'])
    guard = ModelLease(consumer, claimed)
    state.run_service.request_cancel(response['run_id'])
    wall[0] += timedelta(seconds=1)
    assert guard.renew() is False
    assert state.assessment_service.repository.get_record('outbox', event['id'])['lease_until'] == claimed['lease_until']


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_heartbeat_observed_cancellation_settles_when_provider_returns(workspace, kind, monkeypatch):
    from knowpath_backend.learning.model_tasks import ModelLease
    state = workspace()
    _, _, space = seed(state)
    response, event = enqueue(state, space['id'], kind)
    consumer = ModelTaskWorker(state.assessment_service, state.message_service, lease_seconds=3, heartbeat_interval=0.01)
    state.message_service.retriever = KeywordRetriever()
    observed = Event()
    original = ModelLease.renew
    def notify(guard):
        result = original(guard)
        if not result:
            observed.set()
        return result
    monkeypatch.setattr(ModelLease, 'renew', notify)
    class CancelWhileBlocked:
        def generate(self, *args):
            state.run_service.request_cancel(response['run_id'])
            assert observed.wait(2), 'heartbeat ignored cancellation'
            return (FixedQuestions() if kind == 'assessment' else FixedAnswer()).generate(*args)
    state.assessment_service.generator = state.message_service.generator = CancelWhileBlocked()
    consumer.run_once(event['id'])
    assert observed.is_set(), 'heartbeat must observe cancellation before the provider returns'
    assert state.get_run(response['run_id'])['status'] == 'cancelled'
    assert state.assessment_service.repository.get_record('outbox', event['id'])['status'] == 'cancelled'


@pytest.mark.parametrize('value', ['0', '3601', 'nan'])
def test_cli_rejects_unbounded_execution_limit_before_opening_database(monkeypatch, value):
    from knowpath_backend.learning import model_worker_cli as cli
    monkeypatch.setattr(cli, 'create_db_engine', lambda: pytest.fail('invalid limits must not open a database'))
    with pytest.raises(SystemExit) as error:
        cli.main(['--once', '--max-execution-seconds', value])
    assert error.value.code == 2
