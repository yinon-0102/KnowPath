import json
import logging

import pytest

from knowpath_backend.observability.configuration import JsonFormatter, TextFormatter
from knowpath_backend.observability import span
from knowpath_backend.learning.conversations.generation import validate_answer, MessageGenerationError


def test_console_focuses_on_progress_but_json_preserves_ids():
    record = logging.LogRecord('test', logging.INFO, __file__, 1, '', (), None)
    record.event = 'index.batch.completed'
    record.fields = dict(run_id='a-long-run-identifier', request_id='a-long-request-identifier',
                         completed_count=20, total_count=37, batch_number=2, batch_total=4)
    line = TextFormatter(service='graph-worker').format(record)
    assert '20/37' in line and '2/4' in line
    assert 'a-long-run-identifier' not in line and 'a-long-request-identifier' not in line
    assert '事件：' not in line
    document = json.loads(JsonFormatter(service='graph-worker').format(record))
    assert document['run_id'] == record.fields['run_id']


def test_business_stage_start_visible_at_info(caplog):
    caplog.set_level(logging.INFO)
    with span(logging.getLogger('test'), 'material.parse'):
        assert any(getattr(r, 'event', '') == 'material.parse.started' for r in caplog.records)


def test_validation_reports_safe_specific_reason(caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(MessageGenerationError):
        validate_answer({'text': 'private text', 'citation_ids': ['private-source-id']}, [])
    record = next(r for r in caplog.records if getattr(r, 'event', '') == 'message.validation.failed')
    assert record.fields['reason'] == 'unknown_citation'
    assert 'private' not in str(record.fields)


@pytest.mark.parametrize('raw,reason', [
    ({}, 'invalid_structure'),
    ({'text': '', 'citation_ids': ['ok']}, 'invalid_text'),
    ({'text': 'hello', 'citation_ids': []}, 'missing_citations'),
    ({'text': 'hello', 'citation_ids': [['bad']]}, 'unknown_citation'),
    ({'text': 'hello', 'citation_ids': ['ok', 'ok']}, 'duplicate_citation'),
])
def test_validation_reason_does_not_change_error_contract(caplog, raw, reason):
    with pytest.raises(MessageGenerationError) as caught:
        validate_answer(raw, [{'chunk_id': 'ok'}])
    assert caught.value.code == 'MESSAGE_VALIDATION_FAILED'
    assert caplog.records[-1].fields['reason'] == reason


def test_failed_index_write_does_not_report_completion(caplog):
    from knowpath_backend.learning.rag.retrieval import VectorRetriever
    class Embedder:
        def embed(self, texts):
            return [[1.0] for _ in texts]
    class Backend:
        def upsert(self, sources, vectors):
            raise RuntimeError('private provider error')
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError):
        VectorRetriever(Embedder(), Backend()).index([{'text': 'private source'}])
    events = [getattr(r, 'event', '') for r in caplog.records]
    assert 'index.embedding.completed' in events
    assert 'index.write.failed' in events
    assert 'index.write.completed' not in events
    assert 'private' not in str([r.fields for r in caplog.records])
