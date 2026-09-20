"""Command entrypoints exercise real lifecycle adapters and preserve erasure fences.

SQL and Qdrant are local real stores. Only embeddings and environment-owned
client construction are substituted; the adapter and builder execute normally.
"""
from copy import deepcopy
import json

import pytest

from knowpath_backend.learning.rag.registry import ManagedPlugin
from knowpath_backend.test.test_rag_building import build_workspace
from knowpath_backend.test.test_rag_external_cleanup import cleaner, erase, worker


@pytest.mark.parametrize("mode", ["a", "b1"])
def test_cli_build_uses_protocol_prepare_with_real_builder(build_workspace, monkeypatch, tmp_path, mode):
    import dotenv
    import qdrant_client
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.persistence import db
    from knowpath_backend.learning.providers import models
    from knowpath_backend.learning.rag.building import RagBuilder
    from knowpath_backend.learning.rag.cli import main

    state, uploaded, space, repo, embedder, dense, _ = build_workspace
    monkeypatch.setenv("LEARNING_RAG_PLUGIN", "legacy")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    monkeypatch.setattr(db, "create_db_engine", lambda: repo.engine)
    monkeypatch.setattr(models, "embedding_model", lambda settings: embedder)
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **kwargs: dense.client)
    monkeypatch.setattr(dense.client, "close", lambda: None)
    monkeypatch.setattr(LearningSettings, "from_env", classmethod(lambda cls: cls()))
    calls = []
    original = ManagedPlugin.prepare

    def observe(plugin, scope, profile, task_context):
        assert isinstance(plugin.builder, RagBuilder)
        assert plugin.delete_callback is None
        calls.append((plugin.name, scope.scope_snapshot_id, deepcopy(profile), deepcopy(task_context)))
        return original(plugin, scope, profile, task_context)

    monkeypatch.setattr(ManagedPlugin, "prepare", observe)
    config = tmp_path / "build.json"
    output = tmp_path / "manifest.json"
    config.write_text(json.dumps(dict(space_id=space["id"], material_version_id=uploaded.version.id,
        plugin=mode, max_tokens=500, collection_prefix="lifecycle_entrypoint_test")), encoding="utf-8")
    assert main(["build", "--config", str(config), "--output", str(output)]) == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["status"] == "ready" and manifest["a_ready"]
    assert manifest["b1_ready"] is (mode == "b1")
    assert repo.get_manifest(manifest["retrieval_version_id"], manifest["manifest_id"]) == manifest
    assert calls == [(mode, state.space_service.rag_scope_snapshot(space["id"]).scope_snapshot_id,
                      {"max_tokens": 500}, {"material_version_id": uploaded.version.id, "retry": False})]
    with repo.engine.connect() as connection:
        from knowpath_backend.learning.persistence.rag_models import TABLES
        assert connection.execute(TABLES["rag_publications"].select()).first() is None


def test_delete_worker_uses_protocol_after_sql_erasure_and_preserves_other_identities(build_workspace, monkeypatch):
    state, uploaded, space, repo, _, dense, builder = build_workspace
    manifest = builder.build(space["id"], uploaded.version.id)
    rv = manifest["retrieval_version_id"]
    chunk = repo.list_chunks(rv)[0]
    siblings = [dict(chunk, material_version_id="keep-version"),
                dict(chunk, retrieval_version_id="keep-retrieval")]
    dense.upsert(siblings, [[1, 0, 0, 0]] * len(siblings))
    event = erase(state, uploaded)
    calls = []
    original = ManagedPlugin.delete

    def observe(plugin, identity):
        assert state.material_repository.get_material(uploaded.material.id) is None
        assert plugin.builder is None and plugin.implementation is None
        calls.append(deepcopy(identity))
        return original(plugin, identity)

    monkeypatch.setattr(ManagedPlugin, "delete", observe)
    consumer = worker(state, dense)
    assert consumer.run_once(event["id"])
    assert state.graph_service.repository.get_record("outbox", event["id"])["status"] == "completed"
    assert calls == [dict(collection=dense.collection, material_version_id=uploaded.version.id,
                          retrieval_version_id=rv)]
    remaining = dense.client.scroll(dense.collection)[0]
    assert {(row.payload["material_version_id"], row.payload["retrieval_version_id"]) for row in remaining} == {
        ("keep-version", rv), (uploaded.version.id, "keep-retrieval")}


def test_protocol_failure_keeps_authoritative_deletion_pending(build_workspace, monkeypatch):
    state, uploaded, space, repo, _, dense, builder = build_workspace
    manifest = builder.build(space["id"], uploaded.version.id)
    event = erase(state, uploaded)
    calls = []

    def failed_receipt(plugin, identity):
        calls.append(deepcopy(identity))
        return {"status": "failed", "verified": False, "error_code": "PLUGIN_DELETE_FAILED"}

    monkeypatch.setattr(ManagedPlugin, "delete", failed_receipt)
    assert worker(state, dense).run_once(event["id"])
    assert calls, "the worker must inspect the actual plugin deletion receipt"
    assert state.graph_service.repository.get_record("outbox", event["id"])["status"] == "pending"
    assert state.material_repository.get_material(uploaded.material.id) is None
    assert repo.get_retrieval_version(manifest["retrieval_version_id"]) is None
    assert dense.client.count(dense.collection).count > 0


def test_grouped_legacy_cleanup_visits_exact_pairs_once_and_replays_idempotently(build_workspace, monkeypatch):
    _, uploaded, space, repo, _, dense, builder = build_workspace
    manifest = builder.build(space["id"], uploaded.version.id)
    rv = manifest["retrieval_version_id"]
    template = repo.list_chunks(rv)[0]
    versions, retrievals = [uploaded.version.id, "other-version"], [rv, "other-retrieval"]
    pairs = {(mv, retrieval) for mv in versions for retrieval in retrievals}
    rows = [dict(template, material_version_id=mv, retrieval_version_id=retrieval)
            for mv, retrieval in sorted(pairs)]
    rows.extend([dict(template, material_version_id="keep-version"),
                 dict(template, retrieval_version_id="keep-retrieval")])
    rows = [dict(row, chunk_id=f"cleanup-case-{index}") for index, row in enumerate(rows)]
    dense.upsert(rows, [[1, 0, 0, 0]] * len(rows))
    payload = dict(material_id=uploaded.material.id, revision_ids=[], collections=[], rag_indexes=[
        dict(collection=dense.collection, material_version_ids=versions, retrieval_version_ids=retrievals)])
    calls = []
    original = ManagedPlugin.delete

    def observe(plugin, identity):
        calls.append(deepcopy(identity))
        return original(plugin, identity)

    monkeypatch.setattr(ManagedPlugin, "delete", observe)
    consumer = cleaner(dense)
    for _ in range(2):
        calls.clear()
        consumer.delete(payload)
        assert len(calls) == len(pairs)
        assert {(item["material_version_id"], item["retrieval_version_id"]) for item in calls} == pairs
        assert all(item["collection"] == dense.collection for item in calls)
        remaining = dense.client.scroll(dense.collection)[0]
        assert {(row.payload["material_version_id"], row.payload["retrieval_version_id"]) for row in remaining} == {
            ("keep-version", rv), (uploaded.version.id, "keep-retrieval")}


def test_unresolved_writer_blocks_adapter_before_any_external_delete(build_workspace, monkeypatch):
    state, uploaded, space, repo, _, dense, builder = build_workspace
    original_upsert = dense.upsert

    def interrupted(chunks, vectors):
        original_upsert(chunks, vectors)
        raise KeyboardInterrupt("writer ownership lost")

    monkeypatch.setattr(dense, "upsert", interrupted)
    with pytest.raises(KeyboardInterrupt):
        builder.build(space["id"], uploaded.version.id)
    event = erase(state, uploaded)
    calls = []
    monkeypatch.setattr(ManagedPlugin, "delete", lambda *args: calls.append(args))
    assert worker(state, dense).run_once(event["id"])
    assert not calls
    assert dense.client.count(dense.collection).count > 0
    assert state.graph_service.repository.get_record("outbox", event["id"])["status"] == "pending"
