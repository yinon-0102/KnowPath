"""Durable graph staging must never report an unprepared graph as published."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.db import Base, IdempotencyRow, MaterialRow, MaterialVersionRow, SourceChunkRow, RunRow, OutboxEventRow
from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.materials import InMemoryMaterialRepository, MaterialService
from knowpath_backend.learning.repositories import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState


@pytest.fixture(params=["memory", "sqlite", "mysql"])
def graph_workspace(request, tmp_path):
    engine = None
    if request.param == "mysql":
        url = os.getenv("LEARNING_TEST_MYSQL_URL")
        if not url:
            pytest.skip("requires explicit MySQL test URL")
        engine = create_engine(url, pool_pre_ping=True)
    elif request.param == "sqlite":
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'graphs.db'}")
        Base.metadata.create_all(engine)
    memory = InMemoryMaterialRepository()
    label = "graph-test-" + uuid4().hex
    run_ids = set()
    def factory():
        state = LearningState(SqlAlchemyMaterialRepository(engine) if engine else memory)
        original = state.run_service.create
        def tracked(*args, **kwargs):
            run = original(*args, **kwargs)
            run_ids.add(run["id"])
            return run
        state.run_service.create = tracked
        return state
    state = factory()
    upload = state.material_service.create(filename="notes.md", content=("# Functions\n\nReusable functions " + label).encode(), idempotency_key=label)
    try:
        yield factory, upload, label
    finally:
        if engine and request.param == "mysql":
            from knowpath_backend.learning.db import GraphRevisionRow
            with engine.begin() as connection:
                revisions = connection.execute(select(GraphRevisionRow.id, GraphRevisionRow.run_id).where(GraphRevisionRow.material_id == upload.material.id)).all()
                versions = connection.scalars(select(MaterialVersionRow.id).where(MaterialVersionRow.material_id == upload.material.id)).all()
                # A material DELETE already removed its revisions, but its cleanup
                # event remains keyed by material ID and must not outlive the fixture.
                owned_ids = [upload.material.id, *versions, *[r.id for r in revisions]]
                connection.execute(delete(OutboxEventRow).where(OutboxEventRow.aggregate_id.in_(owned_ids)))
                connection.execute(delete(GraphRevisionRow).where(GraphRevisionRow.material_id == upload.material.id))
                connection.execute(delete(RunRow).where(RunRow.id.in_(run_ids)))
                connection.execute(delete(IdempotencyRow).where(IdempotencyRow.key.startswith(label)))
                connection.execute(delete(SourceChunkRow).where(SourceChunkRow.material_version_id.in_(versions)))
                connection.execute(delete(MaterialVersionRow).where(MaterialVersionRow.material_id == upload.material.id))
                connection.execute(delete(MaterialRow).where(MaterialRow.id == upload.material.id))
        if engine:
            engine.dispose()


def stage(factory, upload, label):
    return factory().graph_service.reconcile(upload.material.id, {"version_id": upload.version.id, "expected_graph_version": 0}, label + "-reconcile")


def test_staging_is_durable_atomic_and_replays_original_run(graph_workspace):
    factory, upload, label = graph_workspace
    result = stage(factory, upload, label)
    assert result["status"] == "queued"
    assert stage(factory, upload, label) == result
    state = factory()
    graph = state.graph_service
    diff = graph.diff(upload.material.id)
    assert diff["candidate_revision_id"] == result["candidate_revision_id"]
    assert diff["base_graph_version"] == 0
    assert {row["kind"] for row in diff["added"]} == {"node", "source"}
    assert len(diff["affected_topic_ids"]) == 1
    assert diff["conflicts"] == []
    assert state.get_run(result["run_id"])["status"] == "queued"
    pending = graph.repository.records("outbox", aggregate_id=result["candidate_revision_id"])
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    assert pending[0]["payload"]["snapshot_hash"]
    assert state.material_repository.get_version(upload.version.id).status == "ready"


def test_reconcile_validates_ownership_version_and_key(graph_workspace):
    factory, upload, label = graph_workspace
    graph = factory().graph_service
    with pytest.raises(DomainNotFound):
        graph.reconcile(upload.material.id, {"version_id": str(uuid4()), "expected_graph_version": 0}, label + "-missing")
    with pytest.raises(DomainConflict, match="版本") as exc:
        graph.reconcile(upload.material.id, {"version_id": upload.version.id, "expected_graph_version": 1}, label + "-stale")
    assert exc.value.code == "VERSION_CONFLICT"
    stage(factory, upload, label)
    with pytest.raises(DomainConflict) as exc:
        graph.reconcile(upload.material.id, {"version_id": upload.version.id, "expected_graph_version": 1}, label + "-reconcile")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_reconcile_rolls_back_candidate_run_and_outbox(graph_workspace, monkeypatch):
    factory, upload, label = graph_workspace
    state = factory()
    graph = state.graph_service
    created = []
    original = state.run_service.create
    def record(*args, **kwargs):
        run = original(*args, **kwargs)
        created.append(run["id"])
        return run
    monkeypatch.setattr(state.run_service, "create", record)
    def fail(*args, **kwargs):
        raise RuntimeError("injected replay write failure")
    monkeypatch.setattr(graph.repository, "remember", fail)
    with pytest.raises(RuntimeError, match="injected"):
        graph.reconcile(upload.material.id, {"version_id": upload.version.id, "expected_graph_version": 0}, label + "-rollback")
    assert factory().graph_service.diff(upload.material.id)["candidate_revision_id"] is None
    assert factory().graph_service.repository.records("graph_revisions", material_id=upload.material.id) == []
    with pytest.raises(DomainNotFound):
        factory().get_run(created[0])


def test_parallel_staging_reuses_one_candidate_and_run(graph_workspace):
    factory, upload, label = graph_workspace
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: stage(factory, upload, label), range(2)))
    assert results[0] == results[1]
    assert len(factory().graph_service.repository.records("graph_revisions", material_id=upload.material.id)) == 1


def test_publish_rejects_unprepared_or_missing_candidate(graph_workspace):
    factory, upload, label = graph_workspace
    result = stage(factory, upload, label)
    graph = factory().graph_service
    with pytest.raises(DomainNotFound):
        graph.publish(upload.material.id, str(uuid4()), {"expected_graph_version": 0, "resolutions": []}, label + "-missing-publish")
    with pytest.raises(DomainConflict) as exc:
        graph.publish(upload.material.id, result["candidate_revision_id"], {"expected_graph_version": 0, "resolutions": []}, label + "-publish")
    assert exc.value.code == "REVISION_NOT_READY"
    assert graph.diff(upload.material.id)["status"] == "draft"


def test_diff_compares_against_exact_published_snapshot(graph_workspace):
    factory, upload, label = graph_workspace
    graph = factory().graph_service
    first = stage(factory, upload, label)
    # Seed the future worker/publication boundary; this is not production readiness.
    with graph.repository.transaction():
        baseline = graph.repository.get_record("graph_revisions", first["candidate_revision_id"])
        baseline.update(status="published", graph_version=1)
        graph.repository.put_record("graph_revisions", baseline)
    next_version = factory().material_service.create_version(material_id=upload.material.id, filename="next.md", content=b"# Functions\n\nChanged function semantics.\n\n# Parameters\n\nNamed inputs.", idempotency_key=label + "-next")
    second = factory().graph_service.reconcile(upload.material.id, {"version_id": next_version.version.id, "expected_graph_version": 1}, label + "-reconcile2")
    diff = factory().graph_service.diff(upload.material.id, second["candidate_revision_id"])
    assert diff["base_graph_version"] == 1
    assert any(row["kind"] == "node" for row in diff["added"])
    assert any(row["kind"] == "node" for row in diff["changed"])
    assert any(row["kind"] == "source" for row in diff["removed"])
    assert diff["conflicts"][0]["status"] == "pending"
    assert len(diff["affected_topic_ids"]) == 2
    assert graph.diff(upload.material.id, first["candidate_revision_id"])["candidate_revision_id"] == first["candidate_revision_id"]


def test_graph_http_rejects_invalid_contract_and_fabricated_publish():
    app = create_app()
    material = app.state.learning_state.material_service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="graph-http")
    url = f"/api/v1/materials/{material.material.id}"
    with TestClient(app) as client:
        for payload in ({}, {"version_id": material.version.id, "expected_graph_version": True}, {"version_id": material.version.id, "expected_graph_version": -1}):
            assert client.post(url + "/reconcile", json=payload, headers={"Idempotency-Key": "bad"}).status_code == 422
        staged = client.post(url + "/reconcile", json={"version_id": material.version.id, "expected_graph_version": 0}, headers={"Idempotency-Key": "reconcile"})
        assert staged.status_code == 202
        revision = staged.json()["candidate_revision_id"]
        assert client.get(url + "/graph-diff", params={"revision_id": revision}).json()["added"]
        assert client.get(url + "/graph-diff", params={"revision_id": "missing"}).status_code == 404
        response = client.post(url + f"/graph-revisions/{revision}/publish", json={"expected_graph_version": 0}, headers={"Idempotency-Key": "publish"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REVISION_NOT_READY"
        for resolution in ({"conflict_id": "one", "action": "keep_old"}, {"conflict_id": "one", "action": "use_new", "reason": " "}, {"conflict_id": "one", "action": "bogus"}):
            response = client.post(url + f"/graph-revisions/{revision}/publish", json={"expected_graph_version": 0, "resolutions": [resolution]}, headers={"Idempotency-Key": "invalid-publish"})
            assert response.status_code == 422


def test_material_deletion_cancels_graph_work_and_tombstones_replay(graph_workspace):
    factory, upload, label = graph_workspace
    staged = stage(factory, upload, label)
    state = factory()
    app = create_app(service=state.material_service, run_service=state.run_service)
    with TestClient(app) as client:
        response = client.request("DELETE", f"/api/v1/materials/{upload.material.id}", json={"expected_version": 1, "confirm": True})
        assert response.status_code == 202
    restored = factory()
    assert restored.material_repository.get_material(upload.material.id) is None
    assert restored.get_run(staged["run_id"])["status"] == "cancelled"
    assert restored.graph_service.repository.records("graph_revisions", material_id=upload.material.id) == []
    assert restored.graph_service.repository.records("outbox", aggregate_id=staged["candidate_revision_id"]) == []
    with pytest.raises(DomainConflict) as exc:
        stage(factory, upload, label)
    assert exc.value.code == "RESOURCE_DELETED"


def test_deleted_run_does_not_prevent_material_cleanup(graph_workspace):
    from knowpath_backend.learning.db import RunEventRow
    from knowpath_backend.learning.material_deletion import MaterialDeletionWorker

    factory, upload, _ = graph_workspace
    state = factory()
    result = state.delete_material(upload.material.id, {'expected_version': 1, 'confirm': True})
    repo = state.graph_service.repository
    event = repo.records('outbox', aggregate_id=upload.material.id, event_type='material.delete')[0]
    if hasattr(repo, 'unit_of_work'):
        with repo.transaction(), repo.unit_of_work.session() as session:
            session.execute(delete(RunEventRow).where(RunEventRow.run_id == result['run_id']))
            session.execute(delete(RunRow).where(RunRow.id == result['run_id']))
    else:
        state.run_service.repository._runs.pop(result['run_id'])
    calls = []
    class Cleaner:
        def delete(self, payload):
            calls.append(payload['material_id'])
    restored = factory()
    worker = MaterialDeletionWorker(restored.material_deletion_service, Cleaner())
    assert worker.run_once(event['id'])
    assert repo.get_record('outbox', event['id'])['status'] == 'completed'
    assert calls == [upload.material.id]


def test_identical_versions_report_unchanged_nodes():
    from knowpath_backend.learning.graph_reconciliation import compare_snapshots, extract_snapshot
    state = LearningState()
    uploaded = state.material_service.create(filename="notes.md", content=b"# Topic\n\nAn assertion.", idempotency_key="unchanged")
    snapshot = extract_snapshot(uploaded.material.id, uploaded.version)
    diff = compare_snapshots(snapshot, snapshot)
    assert len(diff["unchanged"]) == 2
    assert not any(diff[key] for key in ("added", "changed", "removed", "conflicts", "affected_topic_ids"))


def test_snapshot_hash_and_conflicts_do_not_depend_on_database_row_order():
    from dataclasses import replace
    from knowpath_backend.learning.graph_reconciliation import compare_snapshots, digest, extract_snapshot
    state = LearningState()
    uploaded = state.material_service.create(filename="ordered.md", content=b"# Functions\n\nFirst paragraph.\n\nSecond paragraph.\n\n# Parameters\n\nNamed values.", idempotency_key="ordered")
    before = extract_snapshot(uploaded.material.id, uploaded.version)
    reversed_version = replace(uploaded.version, chunks=list(reversed(uploaded.version.chunks)))
    after = extract_snapshot(uploaded.material.id, reversed_version)
    assert digest(before) == digest(after)
    diff = compare_snapshots(before, after)
    assert diff["changed"] == diff["conflicts"] == diff["affected_topic_ids"] == []
