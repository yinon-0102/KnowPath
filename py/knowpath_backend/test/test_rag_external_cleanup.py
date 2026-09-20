"""Deletion journals content-v2 destinations and handles late external writes."""
from types import SimpleNamespace

import pytest

from knowpath_backend.learning.materials.deletion import ExternalMaterialCleaner, MaterialDeletionWorker
from knowpath_backend.learning.rag.retrieval import RetrievalError
from knowpath_backend.test.test_rag_building import build_workspace


def cleaner(dense):
    backend = SimpleNamespace(client=dense.client, collection="legacy_unrelated_" + "a" * 16)
    return ExternalMaterialCleaner(SimpleNamespace(graph=None, vectors=SimpleNamespace(backend=backend)))


def erase(state, uploaded):
    state.delete_material(uploaded.material.id, dict(expected_version=1, confirm=True, cascade=True))
    return state.graph_service.repository.records("outbox", event_type="material.delete")[0]


def worker(state, dense):
    return MaterialDeletionWorker(state.material_deletion_service, cleaner(dense))


def test_deletion_journals_custom_content_collection_before_sql_purge(build_workspace):
    state, uploaded, space, repo, _, dense, builder = build_workspace
    manifest = builder.build(space["id"], uploaded.version.id)
    rv = manifest["retrieval_version_id"]
    chunks = repo.list_chunks(rv)
    unrelated = dict(chunks[0], retrieval_version_id="unrelated", material_version_id="keep-version")
    other_version = dict(chunks[0], material_version_id="keep-version")
    other_retrieval = dict(chunks[0], retrieval_version_id="unrelated", chunk_id="other-chunk")
    dense.upsert([unrelated, other_version, other_retrieval], [[1, 0, 0, 0]] * 3)
    event = erase(state, uploaded)
    assert repo.get_retrieval_version(rv) is None
    assert event["payload"]["material_version_ids"] == [uploaded.version.id]
    assert event["payload"]["rag_indexes"] == [dict(collection=dense.collection,
        material_version_ids=[uploaded.version.id], retrieval_version_ids=[rv])]
    assert worker(state, dense).run_once(event["id"])
    cleaner(dense).delete(event["payload"])
    remaining = dense.client.scroll(dense.collection)[0]
    assert {(p.payload["material_version_id"], p.payload["retrieval_version_id"]) for p in remaining} == {
        ("keep-version", "unrelated"), ("keep-version", rv), (uploaded.version.id, "unrelated")}
    assert dense.client.collection_exists(dense.collection)


def test_delete_during_embedding_prevents_vector_write(build_workspace, monkeypatch):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    original = embedder.embed
    def embed(texts, **kwargs):
        event = erase(state, uploaded)
        assert event["payload"]["rag_indexes"][0]["collection"] == dense.collection
        worker(state, dense).run_once(event["id"])
        return original(texts, **kwargs)
    monkeypatch.setattr(embedder, "embed", embed)
    with pytest.raises(RetrievalError, match="INDEX_BUILD_FAILED"):
        builder.build(space["id"], uploaded.version.id)
    assert dense.client.get_collections().collections == []


@pytest.mark.parametrize("compensation_fails", [False, True])
def test_delete_during_upsert_fences_cleanup_until_writer_finishes(build_workspace, monkeypatch, compensation_fails):
    state, uploaded, space, repo, _, dense, builder = build_workspace
    original = dense.upsert
    captured = {}
    def upsert(chunks, vectors):
        captured["event"] = erase(state, uploaded)
        consumer = worker(state, dense)
        consumer.run_once(captured["event"]["id"])
        assert state.graph_service.repository.get_record("outbox", captured["event"]["id"])["status"] == "pending"
        assert captured["event"]["payload"]["rag_write_ids"]
        original(chunks, vectors)  # delayed write lands after the first cleanup sweep
    monkeypatch.setattr(dense, "upsert", upsert)
    if compensation_fails:
        def fail(_):
            raise RuntimeError("secret provider credential")
        monkeypatch.setattr(dense, "delete_retrieval_version", fail)
    with pytest.raises(RetrievalError, match="INDEX_BUILD_FAILED") as error:
        builder.build(space["id"], uploaded.version.id)
    assert "secret" not in str(error.value)
    event = state.graph_service.repository.get_record("outbox", captured["event"]["id"])
    assert event["status"] == "pending"
    if not compensation_fails:
        assert dense.client.count(dense.collection).count == 0
    assert worker(state, dense).run_once(event["id"])
    assert dense.client.count(dense.collection).count == 0


def test_unacknowledged_upsert_keeps_fence_until_remote_writer_stopped(build_workspace, monkeypatch):
    from knowpath_backend.learning.materials.rag_cleanup import reconcile_rag_write
    state, uploaded, space, repo, _, dense, builder = build_workspace
    original = dense.upsert
    def timeout(chunks, vectors):
        original(chunks, vectors)
        raise RuntimeError("remote timeout; completion unknown; secret")
    monkeypatch.setattr(dense, "upsert", timeout)
    with pytest.raises(RetrievalError, match="INDEX_BUILD_FAILED"):
        builder.build(space["id"], uploaded.version.id)
    intents = state.graph_service.repository.records("outbox", event_type="rag.index.write")
    assert len(intents) == 1 and intents[0]["status"] == "processing"
    event = erase(state, uploaded)
    assert worker(state, dense).run_once(event["id"])
    assert state.graph_service.repository.get_record("outbox", event["id"])["status"] == "pending"
    reconcile_rag_write(repo, intents[0]["id"], writer_stopped=True, reason="remote operation completion verified")
    assert worker(state, dense).run_once(event["id"])
    assert dense.client.count(dense.collection).count == 0


def test_unavailable_provider_details_are_not_exposed_by_builder(build_workspace, monkeypatch):
    _, uploaded, space, repo, embedder, _, builder = build_workspace
    def fail(*args, **kwargs):
        raise RuntimeError("https://provider.invalid?api_key=private")
    monkeypatch.setattr(embedder, "embed", fail)
    with pytest.raises(RetrievalError, match="INDEX_BUILD_FAILED") as error:
        builder.build(space["id"], uploaded.version.id)
    assert "private" not in str(error.value)


def test_crashed_writer_blocks_completion_until_explicit_stopped_confirmation(build_workspace, monkeypatch):
    from knowpath_backend.learning.materials import rag_cleanup
    state, uploaded, space, repo, _, dense, builder = build_workspace
    original = dense.upsert
    def crash(chunks, vectors):
        original(chunks, vectors)
        raise KeyboardInterrupt("simulate process loss before intent close")
    monkeypatch.setattr(dense, "upsert", crash)
    # Process loss must leave the durable processing intent, unlike handled errors.
    with pytest.raises(KeyboardInterrupt):
        builder.build(space["id"], uploaded.version.id)
    event = erase(state, uploaded)
    assert len(event["payload"]["rag_write_ids"]) == 1
    identifier = event["payload"]["rag_write_ids"][0]
    consumer = worker(state, dense)
    assert consumer.run_once(event["id"])
    assert state.graph_service.repository.get_record("outbox", event["id"])["status"] == "pending"
    for confirmation in (False, "true", 1):
        with pytest.raises(ValueError, match="writer stopped"):
            rag_cleanup.reconcile_rag_write(repo, identifier, writer_stopped=confirmation, reason="process terminated")
    rag_cleanup.reconcile_rag_write(repo, identifier, writer_stopped=True, reason="process termination verified")
    rag_cleanup.reconcile_rag_write(repo, identifier, writer_stopped=True, reason="idempotent replay")
    assert consumer.run_once(event["id"])
    assert state.graph_service.repository.get_record("outbox", event["id"])["status"] == "completed"
    assert dense.client.count(dense.collection).count == 0
