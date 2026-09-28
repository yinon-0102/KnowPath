"""Operational logs must explain work without exposing learning content or keys."""
import asyncio
import io
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.retrieval import KeywordRetriever
from knowpath_backend.test.test_learning_material_deletion import workspace, seed
from knowpath_backend.test.test_learning_ingestion import apps
from knowpath_backend.test.test_rag_building import build_workspace


def app():
    return create_app(settings=LearningSettings(local_token='logging-test-token'), source_retriever=KeywordRetriever())


def events(caplog, name):
    return [record for record in caplog.records if getattr(record, 'event', None) == name]


def test_requests_log_correlated_completion_without_request_content(caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(app()) as client:
        response = client.get('/api/v1/learning-spaces?private=do-not-log', headers={'X-Local-Token': 'wrong-secret'})
    assert response.status_code == 401
    records = events(caplog, 'http.request.completed')
    assert len(records) == 1
    fields = records[0].fields
    assert fields['request_id'] == response.headers['X-Request-ID'] == response.json()['error']['request_id']
    assert fields['status_code'] == 401 and fields['duration_ms'] >= 0
    assert fields['error_code'] == 'LOCAL_TOKEN_REQUIRED'
    assert all(secret not in str(fields) for secret in ('do-not-log','wrong-secret','logging-test-token'))


def test_unexpected_failure_keeps_safe_trace_and_existing_response(caplog):
    caplog.set_level(logging.DEBUG)
    application = app()
    @application.get('/explode')
    def explode():
        raise RuntimeError('password=never-log-this body=private-learning-content')
    with TestClient(application) as client:
        response = client.get('/explode', headers={'X-Local-Token':'logging-test-token'})
    assert response.status_code == 500 and response.json()['error']['code'] == 'INTERNAL_ERROR'
    record, = events(caplog, 'http.request.failed')
    assert record.fields['request_id'] == response.headers['X-Request-ID']
    assert record.fields['exception']['type'] == 'RuntimeError'
    assert any(frame['function']=='explode' for frame in record.fields['exception']['frames'])
    assert 'never-log-this' not in str(record.fields) and 'private-learning-content' not in str(record.fields)


def test_health_polling_does_not_fill_info_logs(caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(app()) as client:
        assert client.get('/api/v1/health').status_code == 200
    record, = events(caplog, 'http.request.completed')
    assert record.levelno == logging.DEBUG


def test_structured_json_preserves_chinese_and_redacts_nested_credentials(monkeypatch):
    from knowpath_backend.observability import JsonFormatter, log_event, bind_context
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'secret-provider-token')
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(service='test'))
    logger = logging.getLogger('knowpath_backend.logging-test')
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with bind_context(request_id='request-one', run_id='run-one'):
            log_event(logger, 'example.completed', note='中文阶段', nested={'api_key':'secret-provider-token','password':'hidden'}, duration_ms=12.3)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    line, = stream.getvalue().splitlines()
    value = json.loads(line)
    assert value['event'] == 'example.completed' and value['request_id'] == 'request-one'
    assert value['run_id'] == 'run-one' and value['note'] == '中文阶段'
    assert 'secret-provider-token' not in line and 'hidden' not in line


def test_concurrent_contexts_and_exceptions_restore_parent():
    from knowpath_backend.observability import bind_context, context_fields
    async def job(identifier):
        with bind_context(request_id=identifier):
            await asyncio.sleep(0)
            assert context_fields()['request_id'] == identifier
            with pytest.raises(RuntimeError):
                with bind_context(run_id='temporary'):
                    raise RuntimeError()
            assert 'run_id' not in context_fields()
    async def run():
        await asyncio.gather(job('a'), job('b'))
    asyncio.run(run())
    assert context_fields() == {}


def test_exception_details_never_serialize_messages_or_locals():
    from knowpath_backend.observability import safe_exception
    try:
        sensitive_local = 'hidden-source-document'
        raise ValueError(sensitive_local)
    except ValueError as exc:
        details = safe_exception(exc)
    assert details['type'] == 'ValueError' and details['frames']
    assert 'hidden-source-document' not in json.dumps(details)
    assert all(set(frame)=={'file','line','function'} for frame in details['frames'])


def test_configuration_is_idempotent_rotates_files_and_preserves_foreign_handlers(tmp_path):
    from knowpath_backend.observability import LogSettings, configure_logging, shutdown_logging, log_event
    root = logging.getLogger()
    # Earlier entrypoint tests may already have installed our own handlers.
    # Reconfiguration replaces those while preserving host/pytest handlers.
    existing = [handler for handler in root.handlers if not getattr(handler, '_knowpath_owned', False)]
    settings = LogSettings(directory=tmp_path, max_bytes=600, backups=2, console_format='json')
    try:
        configure_logging('test', settings=settings, stream=io.StringIO())
        configure_logging('test', settings=settings, stream=io.StringIO())
        owned = [handler for handler in root.handlers if getattr(handler,'_knowpath_owned',False)]
        assert len(owned) == 2 and all(handler in root.handlers for handler in existing)
        for index in range(20):
            log_event(logging.getLogger('knowpath_backend.test'), 'rotation.check', item_count=index)
        files = list(tmp_path.glob('*.jsonl*'))
        assert 1 < len(files) <= 3
        for path in files:
            for line in path.read_text(encoding='utf-8').splitlines():
                assert json.loads(line)['service']=='test'
    finally:
        shutdown_logging()


@pytest.mark.parametrize('key,value', [('KNOWPATH_LOG_LEVEL','VERBOSE'),('KNOWPATH_LOG_FORMAT','xml'),('KNOWPATH_LOG_MAX_BYTES','0'),('KNOWPATH_LOG_BACKUPS','-1')])
def test_invalid_logging_configuration_fails_clearly(monkeypatch, key, value):
    from knowpath_backend.observability import LogSettings
    monkeypatch.setenv(key,value)
    with pytest.raises(ValueError):
        LogSettings.from_env()


def test_legacy_credentials_are_redacted_in_text_and_exception_details():
    from knowpath_backend.observability import JsonFormatter
    record = logging.LogRecord('legacy',logging.ERROR,__file__,1,
        'Authorization: Bearer abcdef123 password=hidden mysql://user:pw@host/db',(),None)
    value = JsonFormatter(service='test').format(record)
    assert all(secret not in value for secret in ('abcdef123','hidden','user:pw'))


def test_sse_completion_logs_after_last_body_with_context(caplog):
    from fastapi.responses import StreamingResponse
    from knowpath_backend.observability import context_fields
    caplog.set_level(logging.DEBUG)
    application = app()
    seen = []
    @application.get('/stream')
    async def stream():
        async def chunks():
            seen.append(context_fields()['request_id'])
            yield b'data: first\n\n'
            await asyncio.sleep(0)
            assert not events(caplog, 'http.request.completed')
            seen.append(context_fields()['request_id'])
            yield b'data: final\n\n'
        return StreamingResponse(chunks(),media_type='text/event-stream')
    with TestClient(application) as client:
        response=client.get('/stream',headers={'X-Local-Token':'logging-test-token'})
    record,=events(caplog,'http.request.completed')
    assert seen==[response.headers['X-Request-ID']]*2
    assert record.fields['streaming'] is True and record.fields['response_complete'] is True
    assert record.fields['response_bytes']==len(response.content)


def test_provider_retry_has_attempt_status_and_no_payload(caplog, monkeypatch):
    import httpx
    from knowpath_backend.learning.providers.models import DashScopeChatAdapter
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv('DASHSCOPE_API_KEY','provider-private-secret')
    attempts=[]
    def handle(request):
        attempts.append(request)
        return httpx.Response(429) if len(attempts)==1 else httpx.Response(200,json={'choices':[{'message':{'content':'private-answer'},'finish_reason':'stop'}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result=DashScopeChatAdapter(LearningSettings(),client=client).generate([{'role':'user','content':'private-question'}])
    assert result['content']=='private-answer' and len(attempts)==2
    retry,=events(caplog,'model.http.retry')
    assert retry.fields['attempt']==1 and retry.fields['status_code']==429
    completed,=events(caplog,'model.http.completed')
    assert completed.fields['duration_ms']>=0
    for record in caplog.records:
        if hasattr(record,'fields'):
            assert all(value not in str(record.fields) for value in ('provider-private-secret','private-question','private-answer'))


def test_worker_retry_keeps_origin_context_and_reports_pending(workspace,caplog):
    from datetime import datetime, timezone
    from knowpath_backend.test.test_learning_model_tasks import enqueue, worker
    from knowpath_backend.learning.assessments.generation import QuestionGenerationError
    from knowpath_backend.observability import bind_context, context_fields
    caplog.set_level(logging.DEBUG)
    state=workspace()
    _,_,space=seed(state)
    with bind_context(request_id='origin-http'):
        response,event=enqueue(state,space['id'],'assessment')
    class Unavailable:
        def generate(self,*args):
            raise QuestionGenerationError('MODEL_UNAVAILABLE')
    state.assessment_service.generator=Unavailable()
    assert worker(state,datetime.now(timezone.utc)).run_once(event['id'])
    finished,=events(caplog,'worker.attempt.finished')
    assert finished.fields['job_id']==event['id'] and finished.fields['run_id']==response['run_id']
    assert finished.fields['request_id']=='origin-http'
    assert finished.fields['observed_status']=='pending' and finished.fields['attempt']==1
    assert finished.fields['error_code']=='MODEL_UNAVAILABLE'
    assert context_fields()=={}


def test_completed_command_links_request_to_run_without_logging_payload(caplog):
    from knowpath_backend.observability import bind_context
    from knowpath_backend.learning.state import LearningState
    caplog.set_level(logging.DEBUG)
    state=LearningState()
    _,_,space=seed(state)
    caplog.clear()
    with bind_context(request_id='http-origin'):
        result=state.message_service.send(space['id'],{'message':'private-user-message'},'private-key',durable=True)
    record,=events(caplog,'command.completed')
    assert record.fields['run_id']==result['run_id'] and record.fields['request_id']=='http-origin'
    assert 'private-user-message' not in str(record.fields) and 'private-key' not in str(record.fields)


def test_rag_stage_logs_only_counts(caplog):
    from knowpath_backend.learning.rag.pipeline import RagPipeline
    caplog.set_level(logging.DEBUG)
    pipeline = object.__new__(RagPipeline)
    pipeline.last_retrieval_snapshot = None
    pipeline.retrieval_snapshot_callback = None
    pipeline._snapshot(stage='candidate', candidate_ids=['one', 'two'], candidate_sources=[{'source_text':'private-source'}])
    record, = events(caplog, 'rag.stage')
    assert record.fields['stage'] == 'candidate' and record.fields['candidate_count'] == 2
    assert 'private-source' not in str(record.fields)


@pytest.mark.parametrize('role', ['model', 'graph'])
def test_worker_cli_failure_is_safe_and_closes_logging(monkeypatch, capsys, role):
    from importlib import import_module
    cli = import_module('knowpath_backend.learning.workers.' + role + '_cli')
    monkeypatch.setattr(cli, 'load_dotenv', lambda: None)
    monkeypatch.setenv('KNOWPATH_LOG_DIR', '')
    monkeypatch.setenv('KNOWPATH_LOG_FORMAT', 'json')
    def fail():
        raise RuntimeError('private-connection password=unsafe')
    monkeypatch.setattr(cli, 'create_db_engine', fail)
    assert cli.main(['--once']) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    records = [json.loads(line) for line in captured.err.splitlines()]
    failure, = [row for row in records if row['event'] == 'worker.start.failed']
    assert failure['exception']['type'] == 'RuntimeError'
    assert 'private-connection' not in captured.err and 'unsafe' not in captured.err
    assert not any(getattr(h, '_knowpath_owned', False) for h in logging.getLogger().handlers)


def test_logging_handler_failure_does_not_change_domain_result():
    from knowpath_backend.observability import observed
    class Broken(logging.Handler):
        def emit(self, record):
            raise OSError('disk full')
    logger = logging.getLogger(__name__)
    handler = Broken()
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    @observed('example')
    def operation():
        return 42
    try:
        assert operation() == 42
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_dependency_debug_is_suppressed_and_warning_omits_payload():
    from knowpath_backend.observability import LogSettings, configure_logging, shutdown_logging
    stream = io.StringIO()
    try:
        configure_logging('test', settings=LogSettings(level='DEBUG', directory=None, console_format='json'), stream=stream)
        logger = logging.getLogger('httpx')
        logger.debug('private-question')
        logger.warning('private-payload')
        row, = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert row['level'] == 'WARNING' and row['logger'] == 'httpx'
        assert 'private-' not in stream.getvalue()
    finally:
        shutdown_logging()


@pytest.mark.parametrize('phase', ['before_headers', 'last_body'])
def test_asgi_disconnect_is_not_success_or_server_failure(caplog, phase):
    from knowpath_backend.learning.api.request_logging import RequestLoggingMiddleware
    from knowpath_backend.observability import context_fields
    caplog.set_level(logging.DEBUG)
    async def application(scope, receive, send):
        if phase == 'before_headers':
            raise asyncio.CancelledError()
        await send({'type':'http.response.start', 'status':200, 'headers':[]})
        await send({'type':'http.response.body', 'body':b'private-text'})
    async def send(message):
        if message['type'] == 'http.response.body':
            raise BrokenPipeError('private-client')
    async def receive():
        return {'type':'http.disconnect'}
    async def run():
        with pytest.raises((asyncio.CancelledError, BrokenPipeError)):
            await RequestLoggingMiddleware(application)({'type':'http', 'method':'GET'}, receive, send)
        assert context_fields() == {}
    asyncio.run(run())
    record, = events(caplog, 'http.request.disconnected')
    assert not record.fields['response_complete'] and record.fields['response_bytes'] == 0
    assert not events(caplog, 'http.request.failed')


def test_stream_failure_after_headers_has_one_failed_event(caplog):
    from fastapi.responses import StreamingResponse
    caplog.set_level(logging.DEBUG)
    application = app()
    @application.get('/broken-stream')
    async def stream():
        async def chunks():
            yield b'data: first\n\n'
            raise RuntimeError('private-stream-content')
        return StreamingResponse(chunks(), media_type='text/event-stream')
    with TestClient(application, raise_server_exceptions=False) as client:
        client.get('/broken-stream', headers={'X-Local-Token':'logging-test-token'})
    record, = events(caplog, 'http.request.failed')
    # BaseHTTPMiddleware may send its final body marker before re-raising a
    # stream exception. Transport completion must never imply business success.
    assert record.fields['status_code'] == 200 and record.fields['exception']
    assert 'private-stream-content' not in str(record.fields)


def test_worker_observer_accepts_keyword_and_ignores_previous_retry_error(caplog):
    from types import SimpleNamespace
    from knowpath_backend.observability import job_observed
    caplog.set_level(logging.DEBUG)
    row = {'id':'job', 'attempts':2, 'status':'completed', 'payload':{'run_id':'run', 'last_error':{'code':'MODEL_UNAVAILABLE'}}}
    class Worker:
        repository = SimpleNamespace(get_record=lambda *a, **kw: row)
        @job_observed
        def execute(self, claimed):
            return True
    assert Worker().execute(claimed=row) is True
    record, = events(caplog, 'worker.attempt.finished')
    assert record.fields['observed_status'] == 'completed' and record.fields['error_code'] is None


def test_provider_final_failure_records_status_model_and_attempt(caplog, monkeypatch):
    import httpx
    from knowpath_backend.learning.providers.models import DashScopeChatAdapter, ModelError
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'private-provider-key')
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        with pytest.raises(ModelError):
            DashScopeChatAdapter(LearningSettings(), client=client).generate([])
    failure, = events(caplog, 'model.http.failed')
    assert failure.fields['status_code'] == 503 and failure.fields['attempt'] == 3
    assert failure.fields['model'] == 'qwen-plus' and failure.fields['provider'] == 'dashscope'


def test_worker_once_keeps_stdout_machine_readable(monkeypatch, capsys):
    from types import SimpleNamespace
    from knowpath_backend.learning.workers import model_cli as cli
    monkeypatch.setattr(cli, 'load_dotenv', lambda: None)
    monkeypatch.setenv('KNOWPATH_LOG_DIR', '')
    monkeypatch.setenv('KNOWPATH_LOG_FORMAT', 'json')
    monkeypatch.setattr(cli, 'create_db_engine', lambda: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(cli, 'SqlAlchemyMaterialRepository', SimpleNamespace(from_env=lambda _: SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(cli, 'LearningState', lambda _: SimpleNamespace(assessment_service=None, message_service=SimpleNamespace(retriever=None)))
    monkeypatch.setattr(cli, 'ModelTaskWorker', lambda *a, **kw: SimpleNamespace(run_once=lambda: False))
    assert cli.main(['--once']) == 0
    capture = capsys.readouterr()
    assert json.loads(capture.out) == {'job_claimed':False}
    assert [json.loads(line)['event'] for line in capture.err.splitlines()] == ['worker.started','worker.stopped']


def test_unwritable_log_directory_falls_back_to_stderr(tmp_path):
    from knowpath_backend.observability import LogSettings, configure_logging, shutdown_logging, log_event
    path = tmp_path / 'file'
    path.write_text('not a directory')
    stream = io.StringIO()
    try:
        configure_logging('test', settings=LogSettings(directory=path, console_format='json'), stream=stream)
        log_event(logging.getLogger('knowpath_backend.test'), 'still.running')
        assert [json.loads(line)['event'] for line in stream.getvalue().splitlines()] == ['logging.file.unavailable','still.running']
    finally:
        shutdown_logging()


def test_uvicorn_preformatted_traceback_cannot_leak_exception_text():
    from knowpath_backend.observability import JsonFormatter
    record = logging.LogRecord('uvicorn.error', logging.ERROR, __file__, 1,
        'Traceback (most recent call last):\n  private source line\nRuntimeError: PRIVATE_LEARNING_CONTENT', (), None)
    value = JsonFormatter(service='api').format(record)
    assert 'PRIVATE_LEARNING_CONTENT' not in value and 'private source line' not in value
    assert json.loads(value)['logger'] == 'uvicorn.error'


def test_lifespan_cleanup_failure_is_logged_without_message(caplog):
    caplog.set_level(logging.DEBUG)
    application = app()
    def close():
        raise RuntimeError('private-shutdown-content')
    application.state.material_service.repository.close = close
    with pytest.raises(RuntimeError):
        with TestClient(application):
            pass
    failure, = events(caplog, 'api.shutdown.failed')
    assert failure.fields['exception']['type'] == 'RuntimeError'
    assert 'private-shutdown-content' not in str(failure.fields)


@pytest.mark.parametrize('message', ["password='QUOTED_SECRET'", '"api_key": "QUOTED_SECRET"', "{'password': 'QUOTED_SECRET'}"])
def test_legacy_quoted_credentials_are_redacted(message):
    from knowpath_backend.observability import JsonFormatter
    record = logging.LogRecord('legacy', logging.ERROR, __file__, 1, message, (), None)
    assert 'QUOTED_SECRET' not in JsonFormatter(service='test').format(record)


def test_pdf_parser_warning_does_not_log_document_content():
    from types import SimpleNamespace
    from pypdf.generic import DictionaryObject
    from knowpath_backend.observability import LogSettings, configure_logging, shutdown_logging
    stream = io.StringIO()
    try:
        configure_logging('test', settings=LogSettings(directory=None, console_format='json'), stream=stream)
        DictionaryObject.read_from_stream(io.BytesIO(b'<< /PRIVATE_PDF_CONTENT 1 /PRIVATE_PDF_CONTENT 2 >>'), SimpleNamespace(strict=False))
        assert 'PRIVATE_PDF_CONTENT' not in stream.getvalue()
        assert any(row['logger'].startswith('pypdf') for row in map(json.loads, stream.getvalue().splitlines()))
    finally:
        shutdown_logging()


def test_parse_graph_handoff_keeps_originating_http_request(apps, caplog):
    from knowpath_backend.test.test_learning_auto_ingest import upload, parse_worker
    from knowpath_backend.test.test_learning_graph_worker import PreparedBackend
    from knowpath_backend.learning.workers.graph import GraphWorker
    caplog.set_level(logging.DEBUG)
    with TestClient(apps()) as client:
        response = upload(client)
    restored = apps()
    assert parse_worker(restored).run_once()
    assert GraphWorker(restored.state.learning_state.graph_service, PreparedBackend()).run_once()
    finished = events(caplog, 'worker.attempt.finished')
    assert len(finished) == 2
    assert {r.fields['job_type'] for r in finished} == {'material.parse','graph.prepare'}
    assert all(r.fields['request_id'] == response.headers['X-Request-ID'] for r in finished)
    assert all(r.fields['run_id'] == response.json()['run_id'] and r.fields['observed_status'] == 'completed' for r in finished)


def test_rag_logs_real_stage_durations_and_counts(build_workspace, caplog):
    from knowpath_backend.test.test_rag_pipeline import pipeline
    caplog.set_level(logging.DEBUG)
    service, _ = pipeline(build_workspace)
    result = service.answer('学校应该告知谁？', space_id=build_workspace[2]['id'])
    assert result['status'] == 'answered'
    for name in ('rag.retrieve.completed','rag.rerank.completed','rag.verify.completed','rag.answer.completed'):
        record, = events(caplog, name)
        assert record.fields['duration_ms'] >= 0 and record.fields['space_id'] == build_workspace[2]['id']
    assert [record.fields['stage'] for record in events(caplog, 'rag.stage')] == ['candidate','rerank','context']
    record, = events(caplog, 'rag.result')
    assert record.fields['citation_count'] == len(result['citations'])
    assert all('学校' not in str(record.fields) for record in caplog.records if hasattr(record,'fields'))


def test_app_factory_does_not_reconfigure_global_handlers():
    before = list(logging.getLogger().handlers)
    app()
    assert logging.getLogger().handlers == before


@pytest.mark.parametrize('role', ['model', 'graph'])
def test_worker_cleanup_error_has_safe_diagnostic_and_nonzero_exit(monkeypatch, capsys, role):
    from importlib import import_module
    from types import SimpleNamespace
    cli = import_module('knowpath_backend.learning.workers.' + role + '_cli')
    monkeypatch.setattr(cli, 'load_dotenv', lambda: None)
    monkeypatch.setenv('KNOWPATH_LOG_DIR', '')
    monkeypatch.setenv('KNOWPATH_LOG_FORMAT', 'json')
    closed = []
    def close():
        closed.append('materials')
        raise RuntimeError('PRIVATE_CLEANUP_CONTENT')
    monkeypatch.setattr(cli, 'create_db_engine', lambda: SimpleNamespace(dispose=lambda: closed.append('engine')))
    monkeypatch.setattr(cli, 'SqlAlchemyMaterialRepository', SimpleNamespace(from_env=lambda _: SimpleNamespace(close=close)))
    monkeypatch.setattr(cli, 'LearningState', lambda _: (_ for _ in ()).throw(RuntimeError('PRIVATE_STARTUP_CONTENT')))
    assert cli.main(['--once']) == 1
    capture = capsys.readouterr()
    assert closed == ['materials', 'engine']
    assert 'PRIVATE_' not in capture.err
    assert 'worker.shutdown.failed' in capture.err
    assert not any(getattr(h, '_knowpath_owned', False) for h in logging.getLogger().handlers)
