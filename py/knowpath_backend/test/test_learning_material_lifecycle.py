"""Import lifecycle contracts using memory and isolated SQLite only."""
import pytest
from knowpath_backend.test.test_learning_graph_reconciliation import graph_workspace, stage
from knowpath_backend.test.test_learning_graph_worker import worker, event_id
from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
from knowpath_backend.learning.materials.service import MaterialParser


def test_terminal_failure_is_visible_and_retry_has_new_run(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    class Broken:
        def prepare(self, manifest, heartbeat):
            raise RuntimeError('secret-provider-token')
    worker(state, Broken(), max_attempts=1).run_once(event_id(state, staged))
    diff = state.graph_service.diff(upload.material.id)
    assert diff['status'] == 'failed'
    assert diff['error']['details']['stage'] == 'preparing'
    assert diff['error']['details']['retryable'] is True
    assert 'secret' not in str(diff)
    retry = state.graph_service.retry(upload.material.id, {'version_id': upload.version.id}, label+'-retry')
    assert retry['run_id'] != staged['run_id']
    worker(state).run_once(event_id(state, retry))
    assert state.graph_service.lifecycle(upload.material.id)['stage'] == 'pending_review'
    assert state.get_run(staged['run_id'])['status'] == 'failed'


def test_pending_review_is_reused_with_actual_status(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    worker(state).run_once(event_id(state, staged))
    reused = state.graph_service.reconcile(upload.material.id, {'version_id': upload.version.id, 'expected_graph_version': 0}, label+'-again')
    assert reused['candidate_revision_id'] == staged['candidate_revision_id']
    assert reused['status'] == 'succeeded'


def test_default_diff_ignores_previous_failure_after_retry_is_published(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    failed = stage(factory, upload, label)
    class Broken:
        def prepare(self, manifest, heartbeat):
            raise RuntimeError('test index failure')
    worker(state, Broken(), max_attempts=1).run_once(event_id(state, failed))
    retry = state.graph_service.retry(upload.material.id, {'version_id': upload.version.id}, label+'-retry')
    worker(state).run_once(event_id(state, retry))
    replacement = state.graph_service.rebuild(upload.material.id, {'version_id': upload.version.id, 'expected_graph_version': 0}, label+'-rebuild')
    worker(state).run_once(event_id(state, replacement))
    state.graph_service.publish(upload.material.id, replacement['candidate_revision_id'], {'expected_graph_version': 0}, label+'-pub')
    current = state.graph_service.diff(upload.material.id)
    assert current['candidate_revision_id'] is None
    assert current['status'] == 'learnable'
    assert current['error'] is None
    assert state.graph_service.diff(upload.material.id, failed['candidate_revision_id'])['status'] == 'failed'


@pytest.mark.parametrize('parsed', [False, True])
def test_repeat_upload_after_reparse_reuses_current_parser_product(graph_workspace, parsed):
    from knowpath_backend.learning.materials.ingestion import MaterialIngestionService
    factory, upload, label = graph_workspace
    state = factory()
    new = state.graph_service.reparse(upload.material.id, {'version_id': upload.version.id}, label+'-reparse')
    if parsed:
        MaterialParseWorker(state.graph_service).run_once()
    ingestion = MaterialIngestionService(state.material_service, state.run_service, state.graph_service)
    repeated = ingestion.create(filename=upload.version.filename, content=state.material_repository.get_raw(upload.version.id), idempotency_key=label+'-repeat')
    assert repeated.resource.version.id == new['material_version_id']
    assert repeated.resource.material.current_version_id == new['material_version_id']
    assert len(state.material_repository.list_versions(upload.material.id)) == 2


@pytest.mark.parametrize('scoped', [False, True])
def test_hash_dedup_locks_material_before_version_without_loading_unrelated_chunks(graph_workspace, scoped):
    from sqlalchemy import event
    from sqlalchemy.dialects import mysql
    from sqlalchemy.orm import Session
    factory, upload, label = graph_workspace
    repository = factory().material_repository
    if not getattr(repository, 'unit_of_work', None):
        pytest.skip('SQL lock intent contract')
    statements = []
    def capture(execution):
        statement = execution.statement
        if getattr(statement, '_for_update_arg', None) is not None:
            statements.append(str(statement.compile(dialect=mysql.dialect())))
    event.listen(Session, 'do_orm_execute', capture)
    try:
        with repository.transaction():
            found = repository.find_by_content_hash(upload.version.content_hash, filename=upload.version.filename,
                material_id=upload.material.id if scoped else None)
            assert found[1].id == upload.version.id
    finally:
        event.remove(Session, 'do_orm_execute', capture)
    assert 'FROM materials' in statements[0]
    assert 'ORDER BY materials.id' in statements[0]
    assert ('WHERE materials.id =' in statements[0]) == scoped
    assert 'FROM material_versions' in statements[1]
    assert 'material_versions.content_hash' in statements[1]
    assert all('FROM source_chunks' not in sql for sql in statements[:2])


def test_confirmed_correction_still_targets_current_publication_after_raw_upload(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    first = stage(factory, upload, label)
    worker(state).run_once(event_id(state, first))
    state.graph_service.publish(upload.material.id, first['candidate_revision_id'], {'expected_graph_version': 0}, label+'-pub')
    space = state.create_space({'material_ids': [upload.material.id]})
    node = state.space_service.bound_topics(space)[0]
    newer = state.material_service.create_version(material_id=upload.material.id, filename='new.md', content=b'# New\n\nUnpublished content.', idempotency_key=label+'-new', defer_parse=True)
    created = state.create_correction(space['id'], {'kind': 'node', 'target_id': node['id'], 'action': 'replace',
        'reason': 'reviewed published source', 'source_ref': node['source_refs'][0]['chunk_id'], 'proposed_value': {'name': 'Corrected'}})
    confirmed = state.confirm_correction(space['id'], created['correction_id'], {'expected_graph_version': 1, 'reason': 'checked source'})
    worker(state, max_attempts=1).run_once(event_id(state, created))
    assert state.get_run(confirmed['run_id'])['status'] == 'succeeded'
    published = state.graph_service._published(state.graph_service._history(upload.material.id))
    assert published['graph_version'] == 2
    assert published['material_version_id'] == upload.version.id
    assert published['publication']['snapshot']['nodes'][0]['name'] == 'Corrected'
    assert state.material_repository.get_material(upload.material.id).current_version_id == newer.version.id
    assert state.get_space(space['id'])['bindings'][0]['graph_revision_id'] == first['candidate_revision_id']


def test_g17_publication_invalidates_a_legacy_sibling_and_keeps_history(graph_workspace):
    import copy
    from uuid import uuid4
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    worker(state).run_once(event_id(state, staged))
    graph = state.graph_service
    with graph.repository.transaction():
        sibling = copy.deepcopy(graph.repository.get_record('graph_revisions', staged['candidate_revision_id']))
        sibling.update(id=str(uuid4()), sequence=2, run_id=state.run_service.create('graph_reconcile')['id'])
        graph.repository.put_record('graph_revisions', sibling)
    graph.publish(upload.material.id, staged['candidate_revision_id'], {'expected_graph_version': 0}, label+'-publish')
    assert graph.repository.get_record('graph_revisions', sibling['id'])['status'] == 'superseded'
    assert state.get_run(sibling['run_id'])['status'] == 'cancelled'
    assert graph.diff(upload.material.id, sibling['id'])['status'] == 'superseded'
    with pytest.raises(Exception) as exc:
        graph.publish(upload.material.id, sibling['id'], {'expected_graph_version': 1}, label+'-obsolete')
    assert exc.value.code in {'VERSION_CONFLICT', 'REVISION_NOT_READY'}


def test_reparse_creates_new_immutable_version_and_reloads_original(graph_workspace, monkeypatch):
    factory, upload, label = graph_workspace
    state = factory()
    original = state.material_repository.get_version(upload.version.id)
    monkeypatch.setattr(MaterialParser, 'VERSION', 'knowpath_parser_upgrade_test')
    result = state.graph_service.reparse(upload.material.id, {'version_id': original.id}, label+'-reparse')
    assert result['material_version_id'] != original.id
    new = state.material_repository.get_version(result['material_version_id'])
    assert new.parser_version == MaterialParser.VERSION
    assert not new.chunks
    assert state.material_repository.get_raw(new.id) == state.material_repository.get_raw(original.id)
    class Recording(MaterialParser):
        calls = []
        def parse(self, content, *, filename):
            self.calls.append(content)
            return super().parse(content, filename=filename)
    parser = Recording()
    MaterialParseWorker(state.graph_service, parser).run_once()
    assert len(parser.calls) == 1
    assert state.material_repository.get_version(original.id).chunks == original.chunks
    assert state.material_repository.get_version(original.id).parser_version == original.parser_version
    assert {c.id for c in state.material_repository.get_version(new.id).chunks}.isdisjoint(c.id for c in original.chunks)
    assert state.graph_service.reparse(upload.material.id, {'version_id': original.id}, label+'-reparse') == result


def test_replacement_fences_old_lease_and_publication_invalidates_siblings(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    job = worker(state)
    old = job.claim(event_id(state, staged))
    replacement = state.graph_service.rebuild(upload.material.id, {'version_id': upload.version.id, 'expected_graph_version': 0}, label+'-rebuild')
    assert replacement['candidate_revision_id'] != staged['candidate_revision_id']
    assert not job.execute(old)
    assert state.graph_service.repository.get_record('graph_revisions', staged['candidate_revision_id'])['status'] == 'superseded'
    worker(state).run_once(event_id(state, replacement))
    state.graph_service.publish(upload.material.id, replacement['candidate_revision_id'], {'expected_graph_version': 0}, label+'-publish')
    assert state.graph_service.lifecycle(upload.material.id)['stage'] == 'learnable'


def test_reparse_with_inflight_parse_fences_old_version_handoff(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    queued = state.material_service.create_version(material_id=upload.material.id, filename='updated.md', content=b'# Updated\n\nUpdated source.', idempotency_key=label+'-queued', defer_parse=True)
    state.graph_service.ingest(upload.material.id, {'version_id': queued.version.id}, label+'-ingest')
    job = MaterialParseWorker(state.graph_service)
    event = state.graph_service.repository.records('outbox', aggregate_id=queued.version.id)[0]
    claimed = job.claim(event['id'])
    new = state.graph_service.reparse(upload.material.id, {'version_id': queued.version.id}, label+'-reparse')
    assert not job.execute(claimed)
    assert job.run_once()
    assert state.graph_service.diff(upload.material.id)['material_version_id'] == new['material_version_id']


def test_historical_publication_and_idle_parsed_version_have_terminal_projection(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    idle = state.graph_service.lifecycle(upload.material.id)
    assert idle['stage'] == 'pending_review'
    assert idle['next_action'] == 'reconcile'
    first = stage(factory, upload, label)
    worker(state).run_once(event_id(state, first))
    state.graph_service.publish(upload.material.id, first['candidate_revision_id'], {'expected_graph_version': 0}, label+'-pub1')
    newer = state.graph_service.reparse(upload.material.id, {'version_id': upload.version.id}, label+'-new')
    MaterialParseWorker(state.graph_service).run_once()
    candidate = state.graph_service.diff(upload.material.id)
    worker(state).run_once(event_id(state, candidate))
    state.graph_service.publish(upload.material.id, candidate['candidate_revision_id'], {'expected_graph_version': 1}, label+'-pub2')
    assert state.graph_service.lifecycle(upload.material.id, upload.version.id)['stage'] == 'learnable'


def test_cancelled_candidate_is_terminal_and_retryable(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    state.cancel_run(staged['run_id'])
    worker(state).run_once(event_id(state, staged))
    assert state.graph_service.lifecycle(upload.material.id)['stage'] == 'failed'
    assert state.graph_service.diff(upload.material.id)['status'] == 'failed'
    assert state.graph_service.diff(upload.material.id)['error']['details']['retryable'] is True


def test_http_projections_preserve_old_published_sources_during_new_failure(graph_workspace):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    factory, upload, label = graph_workspace
    state = factory()
    first = stage(factory, upload, label)
    worker(state).run_once(event_id(state, first))
    state.graph_service.publish(upload.material.id, first['candidate_revision_id'], {'expected_graph_version': 0}, label+'-pub')
    space = state.create_space({'material_ids': [upload.material.id]}, idempotency_key=label+'-space')
    with TestClient(create_app(service=state.material_service, run_service=state.run_service)) as client:
        url = '/api/v1/materials/'+upload.material.id
        response = client.post(url+'/reparse', json={'version_id': upload.version.id}, headers={'Idempotency-Key': label+'-http'})
        assert response.status_code == 202
        new_id = response.json()['material_version_id']
        MaterialParseWorker(state.graph_service).run_once()
        candidate = state.graph_service.diff(upload.material.id)
        class Broken:
            def prepare(self, manifest, heartbeat):
                raise RuntimeError('private-provider-url')
        worker(state, Broken(), max_attempts=1).run_once(event_id(state, candidate))
        listed = client.get('/api/v1/materials').json()['items'][0]
        detail = client.get(url).json()['material']
        versions = client.get(url+'/versions').json()['items']
        assert listed['lifecycle'] == detail['lifecycle']
        assert detail['lifecycle']['stage'] == 'failed'
        assert detail['lifecycle']['published_available'] is True
        assert next(v for v in versions if v['id'] == new_id)['lifecycle']['error']['details']['stage'] == 'preparing'
        old = client.get(url+'/versions/'+upload.version.id+'/chunks/'+upload.version.chunks[0].id, params={'space_id': space['id']})
        assert old.status_code == 200
        assert old.json()['text'] == upload.version.chunks[0].text
        assert state.get_space(space['id'])['bindings'][0]['graph_revision_id'] == first['candidate_revision_id']


@pytest.mark.parametrize('projection', ['list', 'versions', 'empty_versions', 'detail'])
def test_http_lifecycle_projection_tolerates_material_deleted_after_snapshot(graph_workspace, monkeypatch, projection):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    factory, retained, label = graph_workspace
    state = factory()
    deleted = state.material_service.create(filename='deleted.txt', content=b'concurrent deletion',
        idempotency_key=label+'-deleted', defer_parse=True)
    repository = state.material_repository
    method = {'list': 'list_materials', 'versions': 'list_versions', 'empty_versions': 'get_material', 'detail': 'get_version'}[projection]
    original = getattr(repository, method)
    removed = False

    def snapshot_then_delete(*args, **kwargs):
        nonlocal removed
        result = original(*args, **kwargs)
        matching = not args or args[0] == (deleted.version.id if projection == 'detail' else deleted.material.id)
        if not removed and matching:
            removed = True
            # The first read has finished; simulate a concurrent DELETE commit
            # before this HTTP request starts its next lifecycle transaction.
            repository.delete_material(deleted.material.id)
        return result

    monkeypatch.setattr(repository, method, snapshot_then_delete)
    with TestClient(create_app(service=state.material_service, run_service=state.run_service), raise_server_exceptions=False) as client:
        url = '/api/v1/materials' + ('' if projection == 'list' else '/'+deleted.material.id) + ('/versions' if projection in {'versions', 'empty_versions'} else '')
        response = client.get(url)
    assert removed
    if projection == 'list':
        assert response.status_code == 200
        assert [row['id'] for row in response.json()['items']] == [retained.material.id]
        assert response.json()['items'][0]['lifecycle']['material_version_id'] == retained.version.id
    else:
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'RESOURCE_NOT_FOUND'


@pytest.mark.parametrize('resource', ['material', 'run'])
def test_http_material_list_only_omits_matching_deleted_material(graph_workspace, monkeypatch, resource):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    from knowpath_backend.learning.errors import DomainNotFound
    factory, upload, label = graph_workspace
    state = factory()
    app = create_app(service=state.material_service, run_service=state.run_service)
    def missing_dependency(*args):
        raise DomainNotFound(resource, 'unrelated-missing-id')
    monkeypatch.setattr(app.state.learning_state.graph_service, 'lifecycle', missing_dependency)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get('/api/v1/materials')
    assert response.status_code == 500
    assert response.json()['error']['code'] == 'INTERNAL_ERROR'


def test_http_material_list_metadata_matches_lifecycle_after_concurrent_new_version(graph_workspace, monkeypatch):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    factory, upload, label = graph_workspace
    state = factory()
    repository = state.material_repository
    original = repository.list_materials
    newest = None
    def snapshot_then_upload():
        nonlocal newest
        snapshots = original()
        newest = state.material_service.create_version(material_id=upload.material.id, filename='new.txt', content=b'new committed version',
            idempotency_key=label+'-new-version', defer_parse=True)
        return snapshots
    monkeypatch.setattr(repository, 'list_materials', snapshot_then_upload)
    with TestClient(create_app(service=state.material_service, run_service=state.run_service)) as client:
        response = client.get('/api/v1/materials')
    assert response.status_code == 200
    row = next(item for item in response.json()['items'] if item['id'] == upload.material.id)
    assert row['current_version_id'] == row['lifecycle']['material_version_id'] == newest.version.id
    assert row['status'] == newest.material.status


def test_http_detail_lifecycle_stays_with_consulted_version_after_concurrent_upload(graph_workspace, monkeypatch):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    factory, upload, label = graph_workspace
    state = factory()
    app = create_app(service=state.material_service, run_service=state.run_service)
    consult = app.state.source_access_service.consult
    newest = None
    def consult_then_upload(*args, **kwargs):
        nonlocal newest
        result = consult(*args, **kwargs)
        newest = state.material_service.create_version(material_id=upload.material.id, filename='new.txt',
            content=b'version committed during source consultation', idempotency_key=label+'-during-source', defer_parse=True)
        return result
    monkeypatch.setattr(app.state.source_access_service, 'consult', consult_then_upload)
    with TestClient(app) as client:
        response = client.get('/api/v1/materials/'+upload.material.id)
    assert response.status_code == 200
    body = response.json()
    assert body['material']['current_version_id'] == body['version']['id'] == body['material']['lifecycle']['material_version_id'] == upload.version.id
    assert body['version']['chunks'][0]['id'] == upload.version.chunks[0].id
    assert state.material_repository.get_material(upload.material.id).current_version_id == newest.version.id != upload.version.id
