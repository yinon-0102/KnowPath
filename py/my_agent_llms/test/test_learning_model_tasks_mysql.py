"""MySQL row-lock verification consumes only fixture-owned model task IDs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import pytest
from my_agent_llms.test.test_learning_state_persistence import workspace, FixedQuestions
from my_agent_llms.learning.model_tasks import ModelTaskWorker
from my_agent_llms.learning.question_generation import QuestionGenerationError
from my_agent_llms.learning.vector_retrieval import KeywordRetriever


def sql_state(ctx):
    factory, space_id, _, engine = ctx
    if engine is None or engine.dialect.name != 'mysql':
        pytest.skip('requires explicit real MySQL fixture')
    return factory, space_id


@pytest.mark.parametrize('kind', ['assessment', 'message'])
def test_mysql_concurrent_workers_claim_once_and_survive_restart(workspace, kind):
    factory, space_id = sql_state(workspace)
    state = factory()
    if kind == 'assessment':
        result = state.create_assessment(space_id, {'question_count':5}, durable=True)
    else:
        result = state.send_message(space_id, {'message':'Explain functions'}, durable=True)
    event = next(row for row in state.assessment_service.repository.records('outbox', event_type=kind+'.generate', lock=False)
                 if row['payload'].get('run_id') == result['run_id'])
    instant = datetime.now(timezone.utc)
    def claim():
        other = factory()
        consumer = ModelTaskWorker(other.assessment_service, other.message_service, clock=lambda: instant)
        return consumer, consumer.claim(event['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: claim(), range(2)))
    winners = [(consumer, event) for consumer, event in claimed if event]
    assert len(winners) == 1
    consumer, lease = winners[0]
    consumer.messages.retriever = KeywordRetriever()
    consumer.execute(lease)
    restored = factory()
    assert restored.get_run(result['run_id'])['status'] == 'succeeded'
    persisted = restored.assessment_service.repository.get_record('outbox', event['id'])
    assert persisted['status'] == 'completed' and persisted['attempts'] == 1


def test_mysql_transient_failure_retries_original_run_after_restart(workspace):
    factory, space_id = sql_state(workspace)
    state = factory()
    class Unavailable:
        def generate(self, *args):
            raise QuestionGenerationError('MODEL_UNAVAILABLE')
    state.assessment_service.generator = Unavailable()
    result = state.create_assessment(space_id, {'question_count':5}, durable=True)
    event = state.assessment_service.repository.records('outbox', event_type='assessment.generate', aggregate_id=result['id'])[0]
    instant = datetime.now(timezone.utc)
    first = ModelTaskWorker(state.assessment_service, state.message_service, clock=lambda: instant)
    assert first.run_once(event['id'])
    assert state.assessment_service.repository.get_record('outbox', event['id'])['status'] == 'pending'
    restored = factory()
    second = ModelTaskWorker(restored.assessment_service, restored.message_service,
                             clock=lambda: instant+timedelta(seconds=10))
    assert second.run_once(event['id'])
    assert factory().get_run(result['run_id'])['status'] == 'succeeded'
    assert factory().assessment_service.repository.get_record('outbox', event['id'])['attempts'] == 2
