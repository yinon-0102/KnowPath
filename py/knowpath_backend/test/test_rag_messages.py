"""V2 RAG results use the durable message publication guards."""
import pytest

from knowpath_backend.test.test_rag_building import build_workspace
from knowpath_backend.test.test_rag_pipeline import pipeline
from knowpath_backend.learning.rag.verification import VerificationError


def test_message_publishes_verified_v2_answer_and_safe_source_event(build_workspace):
    state, uploaded, space, *_ = build_workspace
    service, _ = pipeline(build_workspace)
    state.message_service.rag_pipeline = service
    response = state.send_message(space['id'], {'message': '学校应该告知谁？'})
    run = state.get_run(response['run_id'])
    assert run['status'] == 'succeeded', run.get('error')
    completed = next(e['data'] for e in run['events'] if e['event'] == 'message.completed')
    assert completed['answer_status'] == 'answered'
    assert completed['citations'][0]['citation_schema_version'] == 2
    refs = next(e['data']['source_refs'] for e in run['events'] if e['event'] == 'tool.completed')
    assert refs and all('source_text' not in r and 'retrieval_text' not in r and 'text' not in r for r in refs)
    record = state.message_service.repository.get_record('messages', completed['message_id'])
    assert record['snapshot']['rag_trace']['manifest_ids']


@pytest.mark.parametrize('status', ['partial', 'insufficient', 'clarify'])
def test_nonfull_answer_status_survives_message_publication(build_workspace, status):
    state, uploaded, space, *_ = build_workspace
    class Pipeline:
        def answer(self, question, **kwargs):
            assert kwargs['expected_scope_version'] == space['scope_version']
            assert kwargs['expected_bindings'] == space['bindings']
            assert not kwargs['cancelled']()
            return {'text': '当前检索证据不足。', 'status': status, 'citations': [], 'sources': [], 'trace': {}}
    state.message_service.rag_pipeline = Pipeline()
    response = state.send_message(space['id'], {'message': '学校？'})
    run = state.get_run(response['run_id'])
    assert run['status'] == 'succeeded'
    result = next(e['data'] for e in run['events'] if e['event'] == 'message.completed')
    assert result['answer_status'] == status and result['citations'] == []


def test_verification_failure_remains_a_failure(build_workspace):
    state, uploaded, space, *_ = build_workspace
    class Pipeline:
        def answer(self, *args, **kwargs):
            raise VerificationError('VERIFICATION_UNAVAILABLE')
    state.message_service.rag_pipeline = Pipeline()
    response = state.send_message(space['id'], {'message': '学校？'})
    run = state.get_run(response['run_id'])
    assert run['status'] == 'failed'
    assert run['error']['code'] == 'VERIFICATION_UNAVAILABLE'
    assert not any(e['event'] == 'message.completed' for e in run['events'])


def test_durable_v2_execution_marker_is_committed_before_provider_call(build_workspace):
    from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
    from knowpath_backend.learning.state import LearningState
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    state, _, space, *_ = build_workspace
    engine = state.material_service.repository.unit_of_work.engine
    observed = []

    class Pipeline:
        def answer(self, *args, **kwargs):
            # Read using another state/repository, so an uncommitted local
            # snapshot mutation cannot satisfy this durability assertion.
            observer = LearningState(SqlAlchemyMaterialRepository(engine))
            record = observer.message_service.repository.get_record('messages', event['aggregate_id'])
            observed.append(record['snapshot'])
            return {'text': '当前检索证据不足。', 'status': 'insufficient', 'citations': [], 'sources': [], 'trace': {}}

    state.message_service.rag_pipeline = Pipeline()
    response = state.message_service.send(space['id'], {'message': '学校应该告知谁？'}, durable=True)
    event = state.message_service.repository.records('outbox', event_type='message.generate')[0]
    assert ModelTaskWorker(messages=state.message_service).run_once(event['id'])
    assert observed[0].get('rag_mode') is True
    assert observed[0].get('rag_execution_started') is True
    assert state.get_run(response['run_id'])['status'] == 'succeeded'


@pytest.mark.parametrize('pipeline_still_configured', [True, False])
def test_crashed_durable_v2_execution_fails_replay_without_repeating_provider(build_workspace, pipeline_still_configured):
    from datetime import datetime, timedelta, timezone
    from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
    from knowpath_backend.learning.state import LearningState
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    state, _, space, *_ = build_workspace
    engine = state.material_service.repository.unit_of_work.engine
    calls = []

    class ProcessCrash(BaseException):
        pass

    class Pipeline:
        def answer(self, *args, **kwargs):
            calls.append('provider')
            if len(calls) == 1:
                # The provider may have already charged/completed its request;
                # no result or model-call receipt reached message publication.
                raise ProcessCrash()
            return {'text': '不应发布重放回答', 'status': 'answered', 'citations': [], 'sources': [], 'trace': {}}

    state.message_service.rag_pipeline = Pipeline()
    response = state.message_service.send(space['id'], {'message': '学校应该告知谁？'}, durable=True)
    event = state.message_service.repository.records('outbox', event_type='message.generate')[0]
    now = datetime.now(timezone.utc)
    with pytest.raises(ProcessCrash):
        ModelTaskWorker(messages=state.message_service, clock=lambda: now, lease_seconds=2).run_once(event['id'])
    assert state.message_service.repository.get_record('messages', event['aggregate_id'])['status'] == 'generating'

    restored = LearningState(SqlAlchemyMaterialRepository(engine))
    restored.message_service.rag_pipeline = Pipeline() if pipeline_still_configured else None
    legacy_calls = []
    class LegacyMustNotRun:
        def select(self, *args, **kwargs):
            legacy_calls.append('retrieval')
            raise RuntimeError('recovered v2 request must not fall back to legacy')
    restored.message_service.retriever = LegacyMustNotRun()
    worker = ModelTaskWorker(messages=restored.message_service,
        clock=lambda: now + timedelta(seconds=3), lease_seconds=2)
    assert worker.run_once(event['id'])
    run = restored.get_run(response['run_id'])
    assert calls == ['provider'] and legacy_calls == []
    assert run['status'] == 'failed'
    assert run['error']['code'] == 'RAG_EXECUTION_INTERRUPTED'
    assert run['error']['retryable'] is False
    assert not any(e['event'] in {'message.delta', 'message.completed'} for e in run['events'])
    stored = restored.message_service.repository.get_record('messages', event['aggregate_id'])
    assert stored['status'] == 'failed'
    assert stored['snapshot']['rag_execution_started'] is True
    settled = restored.message_service.repository.get_record('outbox', event['id'])
    assert settled['status'] == 'failed' and settled['attempts'] == 2
    assert worker.run_once(event['id']) is False


def test_durable_assessment_hint_stays_legacy_without_v2_execution_marker(build_workspace):
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions, create
    state, _, space, *_ = build_workspace
    state.assessment_service.generator = FixedQuestions()
    assessment = create(state, space['id'])
    calls = []
    class MustNotCall:
        def answer(self, *args, **kwargs):
            calls.append('v2')
            raise RuntimeError('assessment hints do not use v2')
        def generate(self, *args, **kwargs):
            calls.append('legacy-model')
            raise RuntimeError('assessment hints do not use a model')
    state.message_service.rag_pipeline = state.message_service.generator = MustNotCall()
    response = state.message_service.send(space['id'],
        {'message': 'Give the answer for ' + assessment['questions'][0]['id']}, durable=True)
    event = state.message_service.repository.records('outbox', event_type='message.generate')[0]
    assert ModelTaskWorker(messages=state.message_service).run_once(event['id'])
    stored = state.message_service.repository.get_record('messages', event['aggregate_id'])
    assert stored['snapshot']['hint'] is True
    assert 'rag_execution_started' not in stored['snapshot'] and 'rag_mode' not in stored['snapshot']
    assert calls == []
    assert state.get_run(response['run_id'])['status'] == 'succeeded'
    audited = state.assessment_service.repository.get_record('assessments', assessment['id'])
    assert audited['questions'][0]['assisted'] is True


def test_durable_v2_transient_failure_does_not_enqueue_an_automatic_retry(build_workspace):
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    state, _, space, *_ = build_workspace
    calls = []
    class Pipeline:
        def answer(self, *args, **kwargs):
            calls.append('provider')
            raise VerificationError('MODEL_UNAVAILABLE')
    state.message_service.rag_pipeline = Pipeline()
    response = state.message_service.send(space['id'], {'message': '学校应该告知谁？'}, durable=True)
    event = state.message_service.repository.records('outbox', event_type='message.generate')[0]
    worker = ModelTaskWorker(messages=state.message_service)
    assert worker.run_once(event['id'])
    assert calls == ['provider']
    assert state.get_run(response['run_id'])['error']['code'] == 'MODEL_UNAVAILABLE'
    settled = state.message_service.repository.get_record('outbox', event['id'])
    assert settled['status'] == 'failed' and settled['attempts'] == 1
    assert worker.run_once(event['id']) is False
