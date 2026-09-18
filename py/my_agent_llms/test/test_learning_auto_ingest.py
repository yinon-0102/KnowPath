"""Real deferred upload -> durable parsing -> existing graph preparation."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from my_agent_llms.test.test_learning_ingestion import apps
from my_agent_llms.test.test_learning_graph_worker import PreparedBackend
from my_agent_llms.learning.graph_worker import GraphWorker


def upload(client, *, key='auto-upload', content=b'# Notes\n\nUseful material.', auto=True):
    return client.post('/api/v1/materials', files={'file': ('notes.md', content, 'text/markdown')},
                       data={'auto_ingest': str(auto).lower()}, headers={'Idempotency-Key': key})


def parse_worker(app, **kwargs):
    from my_agent_llms.learning.material_ingest_worker import MaterialParseWorker
    return MaterialParseWorker(app.state.learning_state.graph_service, **kwargs)


def test_default_upload_is_durable_queued_and_one_run_spans_both_stages(apps):
    app = apps()
    with TestClient(app) as client:
        response = upload(client)
        assert response.status_code == 201
        data = response.json()
        version_id, material_id, run_id = data['version']['id'], data['material']['id'], data['run_id']
        assert data['version']['chunk_count'] == 0
        assert data['version']['status'] == 'processing'
        assert client.get('/api/v1/runs/' + run_id).json()['status'] == 'queued'
    restored = apps()
    state = restored.state.learning_state
    assert state.material_repository.get_raw(version_id) == b'# Notes\n\nUseful material.'
    assert parse_worker(restored).run_once()
    assert state.get_run(run_id)['status'] == 'running'
    assert state.material_repository.get_version(version_id).chunks
    candidates = state.graph_service._history(material_id)
    assert len(candidates) == 1 and candidates[0]['run_id'] == run_id
    assert GraphWorker(state.graph_service, PreparedBackend()).run_once()
    assert state.get_run(run_id)['status'] == 'succeeded'
    assert state.graph_service._history(material_id)[0]['status'] == 'pending_review'
    assert not parse_worker(apps()).run_once()
    with TestClient(apps()) as client:
        replay = upload(client).json()
        assert replay['run_id'] == run_id
        assert len(client.app.state.learning_state.graph_service._history(material_id)) == 1


def test_manual_upload_skips_parser_and_explicit_ingest_uses_saved_original(apps, monkeypatch):
    from my_agent_llms.learning.materials import MaterialParser
    with monkeypatch.context() as patch:
        patch.setattr(MaterialParser, 'parse', lambda *a, **k: pytest.fail('upload must not parse'))
        with TestClient(apps()) as client:
            response = upload(client, auto=False)
            assert response.status_code == 201
            data = response.json()
            assert data['run_id'] is None
            assert data['version']['status'] == 'uploaded'
            assert data['version']['chunk_count'] == 0
            assert not client.app.state.learning_state.graph_service.repository.records('outbox', event_type='material.parse')
    app = apps()
    with TestClient(app) as client:
        path = '/api/v1/materials/' + data['material']['id'] + '/ingest'
        response = client.post(path, json={'version_id': data['version']['id']}, headers={'Idempotency-Key': 'manual-start'})
        assert response.status_code == 202
        assert response.json()['status'] == 'queued'
        assert parse_worker(app).run_once()
        assert app.state.learning_state.material_repository.get_version(data['version']['id']).chunks


def test_parse_failure_persists_safe_error_and_new_key_retries_original(apps):
    app = apps()
    with TestClient(app) as client:
        data = upload(client, content=b'').json()
    assert parse_worker(app).run_once()
    state = apps().state.learning_state
    run = state.get_run(data['run_id'])
    assert run['status'] == 'failed' and run['error']['code'] == 'EMPTY_MATERIAL'
    assert state.material_repository.get_version(data['version']['id']).status == 'failed'
    assert state.material_repository.get_material(data['material']['id']).status == 'failed'
    assert state.material_repository.get_raw(data['version']['id']) == b''
    with TestClient(apps()) as client:
        retry = client.post('/api/v1/materials/' + data['material']['id'] + '/ingest',
                            json={'version_id': data['version']['id']}, headers={'Idempotency-Key': 'parse-retry'})
        assert retry.status_code == 202 and retry.json()['run_id'] != data['run_id']
        assert upload(client, content=b'').json()['run_id'] == data['run_id']


def test_upload_flags_are_part_of_idempotency_and_duplicate_content_coalesces(apps):
    with TestClient(apps()) as client:
        first = upload(client).json()
        assert upload(client, auto=False).status_code == 409
        second = upload(client, key='same-content').json()
        assert second['version']['id'] == first['version']['id']
        assert second['run_id'] == first['run_id']


def test_cancel_during_parse_does_not_commit_sources_or_enqueue_graph(apps):
    app = apps()
    with TestClient(app) as client:
        data = upload(client).json()
    from my_agent_llms.learning.materials import MaterialParser
    state = app.state.learning_state
    class Cancelling(MaterialParser):
        def parse(self, *args, **kwargs):
            state.cancel_run(data['run_id'])
            return super().parse(*args, **kwargs)
    assert parse_worker(app, parser=Cancelling()).run_once()
    assert state.get_run(data['run_id'])['status'] == 'cancelled'
    assert not state.material_repository.get_version(data['version']['id']).chunks
    assert not state.graph_service._history(data['material']['id'])


def test_expired_parse_lease_is_fenced_and_restart_can_take_over(apps):
    app = apps()
    with TestClient(app) as client:
        data = upload(client).json()
    clock = [datetime.now(timezone.utc)]
    first = parse_worker(app, clock=lambda: clock[0], lease_seconds=1)
    event = app.state.learning_state.graph_service.repository.records('outbox', event_type='material.parse')[0]
    abandoned = first.claim(event['id'])
    clock[0] += timedelta(seconds=2)
    successor = parse_worker(apps(), clock=lambda: clock[0], lease_seconds=1)
    claimed = successor.claim(event['id'])
    assert claimed and not first.execute(abandoned)
    assert successor.execute(claimed)
    assert len(app.state.learning_state.graph_service._history(data['material']['id'])) == 1


def test_parse_transaction_failure_never_commits_chunks_without_graph_job(apps, monkeypatch):
    app = apps()
    with TestClient(app) as client:
        data = upload(client).json()
    state = app.state.learning_state
    original = state.graph_service.repository.put_record
    def fail_graph(table, row):
        if table == 'outbox' and row['event_type'] == 'graph.prepare':
            raise RuntimeError('private database details')
        return original(table, row)
    with monkeypatch.context() as patch:
        patch.setattr(state.graph_service.repository, 'put_record', fail_graph)
        assert parse_worker(app, max_attempts=1).run_once()
    assert state.get_run(data['run_id'])['status'] == 'failed'
    assert 'private' not in str(state.get_run(data['run_id']))
    assert not state.material_repository.get_version(data['version']['id']).chunks
    assert not state.graph_service._history(data['material']['id'])


def test_old_upload_key_keeps_legacy_run_after_upgrade(apps):
    from my_agent_llms.learning.materials import MaterialService
    app = apps()
    state = app.state.learning_state
    original = MaterialService(state.material_repository).create(filename='notes.md', content=b'# Notes\n\nUseful material.', idempotency_key='auto-upload')
    old = state.run_service.create('material_ingest', {'id': original.version.id}, status='succeeded')
    state.material_repository.bind_idempotency_run('auto-upload', old['id'])
    with TestClient(apps()) as client:
        assert upload(client).json()['run_id'] == old['id']
        assert upload(client, auto=False).status_code == 409
    assert not state.graph_service.repository.records('outbox', event_type='material.parse')


def test_version_upload_is_deferred_and_note_is_in_fingerprint(apps):
    app = apps()
    with TestClient(app) as client:
        initial = upload(client, auto=False).json()
        path = '/api/v1/materials/' + initial['material']['id'] + '/versions'
        def send(note):
            return client.post(path, files={'file': ('next.txt', b'New content')},
                data={'auto_ingest': 'false', 'change_note': note}, headers={'Idempotency-Key': 'v2'})
        second = send('Updated chapter').json()
        assert second['run_id'] is None and second['status'] == 'uploaded'
        assert send('Different note').status_code == 409
        assert state_version(app, second['material_version_id']).chunks == []


def state_version(app, version_id):
    return app.state.learning_state.material_repository.get_version(version_id)


def test_old_version_failure_does_not_replace_current_or_unarchive_material(apps):
    app = apps()
    with TestClient(app) as client:
        first = upload(client, content=b'').json()
        path = '/api/v1/materials/' + first['material']['id'] + '/versions'
        second = client.post(path, files={'file': ('next.txt', b'Current content')},
            data={'auto_ingest': 'false'}, headers={'Idempotency-Key': 'v2'}).json()
    assert parse_worker(app).run_once()
    repo = app.state.learning_state.material_repository
    assert repo.get_material(first['material']['id']).status == 'uploaded'
    assert repo.get_material(first['material']['id']).current_version_id == second['material_version_id']
    material = repo.get_material(first['material']['id'])
    material.status = 'archived'
    repo.update_material(material)
    with TestClient(app) as client:
        client.post('/api/v1/materials/' + material.id + '/ingest',
            json={'version_id': second['material_version_id']}, headers={'Idempotency-Key': 'archive-parse'})
    assert parse_worker(app).run_once()
    assert repo.get_material(material.id).status == 'archived'


def test_deletion_during_parse_cannot_resurrect_original_or_sources(apps):
    from my_agent_llms.learning.materials import MaterialParser
    app = apps()
    with TestClient(app) as client:
        data = upload(client).json()
        class Deleting(MaterialParser):
            def parse(self, *args, **kwargs):
                assert client.request("DELETE", '/api/v1/materials/' + data['material']['id'], json={"expected_version": 1, "confirm": True}).status_code == 202
                return super().parse(*args, **kwargs)
        assert parse_worker(app, parser=Deleting()).run_once()
    state = apps().state.learning_state
    assert state.material_repository.get_raw(data['version']['id']) is None
    assert state.material_repository.get_version(data['version']['id']) is None
    assert not state.graph_service.repository.records('outbox', event_type='material.parse')
    assert state.get_run(data['run_id'])['status'] == 'cancelled'


def test_cancel_then_new_key_can_retry_before_old_lease_expires(apps):
    app = apps()
    with TestClient(app) as client:
        data = upload(client).json()
        state = app.state.learning_state
        event = state.graph_service.repository.records('outbox', event_type='material.parse')[0]
        abandoned = parse_worker(app).claim(event['id'])
        state.cancel_run(data['run_id'])
        retry = client.post('/api/v1/materials/' + data['material']['id'] + '/ingest',
            json={'version_id': data['version']['id']}, headers={'Idempotency-Key': 'new-key'}).json()
        assert retry['run_id'] != data['run_id']
        assert not parse_worker(app).execute(abandoned)
        assert state_version(app, data['version']['id']).status == 'processing'
        assert parse_worker(app).run_once()
        assert state_version(app, data['version']['id']).status == 'ready'


def test_transient_parse_failure_retries_only_after_backoff(apps):
    from my_agent_llms.learning.materials import MaterialParser
    app = apps()
    with TestClient(app) as client:
        data = upload(client).json()
    clock = [datetime.now(timezone.utc)]
    class FailsOnce(MaterialParser):
        attempts = 0
        def parse(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError('private provider details')
            return super().parse(*args, **kwargs)
    worker = parse_worker(app, parser=FailsOnce(), clock=lambda: clock[0])
    assert worker.run_once()
    assert not worker.run_once()
    assert state_version(app, data['version']['id']).chunks == []
    clock[0] += timedelta(seconds=3)
    assert worker.run_once()
    assert state_version(app, data['version']['id']).chunks


def test_new_version_of_archived_material_preserves_archive(apps):
    app = apps()
    with TestClient(app) as client:
        data = upload(client, auto=False).json()
        repo = app.state.learning_state.material_repository
        material = repo.get_material(data['material']['id'])
        material.status = 'archived'
        repo.update_material(material)
        response = client.post('/api/v1/materials/' + material.id + '/versions',
            files={'file': ('next.txt', b'Another version')}, headers={'Idempotency-Key': 'v2'})
        assert response.status_code == 201
        assert repo.get_material(material.id).status == 'archived'


@pytest.mark.parametrize('new_version', [False, True])
def test_correcting_upload_format_does_not_reuse_failed_parser(apps, new_version):
    app = apps()
    with TestClient(app) as client:
        raw = b'# Valid text\n\nThis is not a PDF.'
        bad = client.post('/api/v1/materials', files={'file': ('wrong.pdf', raw)},
            headers={'Idempotency-Key': 'bad-format'}).json()
        assert parse_worker(app).run_once()
        assert app.state.learning_state.get_run(bad['run_id'])['status'] == 'failed'
        path = '/api/v1/materials/' + bad['material']['id'] + '/versions' if new_version else '/api/v1/materials'
        good_response = client.post(path, files={'file': ('correct.md', raw)},
            headers={'Idempotency-Key': 'good-format'})
        assert good_response.status_code == 201
        good = good_response.json()
        good_id = good['material_version_id'] if new_version else good['version']['id']
        assert good_id != bad['version']['id']
        assert parse_worker(app).run_once()
        assert state_version(app, good_id).status == 'ready'
        duplicate = client.post(path, files={'file': ('correct.txt', raw)},
            headers={'Idempotency-Key': 'same-parser'}).json()
        assert (duplicate['material_version_id'] if new_version else duplicate['version']['id']) == good_id


def test_delete_cancels_only_selected_material_jobs(apps):
    app = apps()
    with TestClient(app) as client:
        first = upload(client).json()
        second = upload(client, key='other-material', content=b'Second material').json()
        assert client.request("DELETE", '/api/v1/materials/' + first['material']['id'], json={"expected_version": 1, "confirm": True}).status_code == 202
    state = app.state.learning_state
    assert state.get_run(second['run_id'])['status'] == 'queued'
    assert state.material_repository.get_raw(second['version']['id']) == b'Second material'
    assert parse_worker(app).run_once()
    assert state_version(app, second['version']['id']).chunks


def test_oversized_upload_is_rejected_before_queuing(apps):
    with TestClient(apps()) as client:
        response = upload(client, content=b'x' * (20 * 1024 * 1024 + 1))
        assert response.status_code == 413
        assert response.json()['error']['code'] == 'MATERIAL_TOO_LARGE'
        assert client.get('/api/v1/materials').json()['items'] == []


def test_worker_cli_once_advances_one_durable_stage_per_invocation(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from my_agent_llms.learning import graph_worker_cli as cli
    from my_agent_llms.learning.api import create_app
    from my_agent_llms.learning.db import init_db
    from my_agent_llms.learning.materials import MaterialService
    from my_agent_llms.learning.repositories import SqlAlchemyMaterialRepository
    engine = create_engine('sqlite+pysqlite:///' + (tmp_path / 'cli.db').as_posix())
    init_db(engine)
    app = create_app(MaterialService(SqlAlchemyMaterialRepository(engine)))
    closed = []
    class Backend(PreparedBackend):
        graph = None
        vectors = type("Vectors", (), {"backend": None})()
        def close(self):
            closed.append(True)
    monkeypatch.setattr(cli, 'create_db_engine', lambda: engine)
    monkeypatch.setattr(cli, 'configured_graph_preparer', lambda repo: Backend())
    monkeypatch.setattr(cli, 'load_dotenv', lambda: None)
    try:
        with TestClient(app) as client:
            data = upload(client).json()
        assert cli.main(['--once']) == 0
        assert app.state.learning_state.get_run(data['run_id'])['status'] == 'running'
        assert cli.main(['--once']) == 0
        assert app.state.learning_state.get_run(data['run_id'])['status'] == 'succeeded'
        assert len(closed) == 2
        assert cli.main(['--once']) == 0
    finally:
        engine.dispose()
