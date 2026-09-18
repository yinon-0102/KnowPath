"""Durable graph jobs and publication, with external I/O replaced at the boundary."""
from datetime import datetime, timedelta, timezone

import pytest

from my_agent_llms.learning.errors import DomainConflict
from my_agent_llms.test.test_learning_graph_reconciliation import graph_workspace, stage


class PreparedBackend:
    def prepare(self, manifest, heartbeat):
        heartbeat()
        return {"manifest_hash": manifest["manifest_hash"], "neo4j": {"verified": True},
                "qdrant": {"verified": True, "collection": "test"}}


def worker(state, backend=None, **kwargs):
    from my_agent_llms.learning.graph_worker import GraphWorker
    return GraphWorker(state.graph_service, backend or PreparedBackend(), **kwargs)


def event_id(state, staged):
    return state.graph_service.repository.records("outbox", aggregate_id=staged["candidate_revision_id"])[0]["id"]


def test_worker_prepares_and_publish_replays_without_moving_existing_spaces(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    # A space that predates GraphService publication must retain its legacy binding.
    space = state.create_space({"material_ids": [upload.material.id]}, idempotency_key=label+"-space")
    try:
        staged = stage(factory, upload, label)
        assert worker(state).run_once(event_id(state, staged))
        restored = factory()
        revision = restored.graph_service.repository.get_record("graph_revisions", staged["candidate_revision_id"])
        assert revision["status"] == "pending_review"
        assert restored.get_run(staged["run_id"])["status"] == "succeeded"
        assert not worker(restored).run_once(event_id(restored, staged))
        payload = {"expected_graph_version": 0, "resolutions": []}
        published = restored.graph_service.publish(upload.material.id, revision["id"], payload, label+"-publish")
        assert published["graph_version"] == 1
        assert published["material_version_id"] == upload.version.id
        assert published["published_at"]
        assert factory().graph_service.publish(upload.material.id, revision["id"], payload, label+"-publish") == published
        assert restored.get_space(space["id"])["bindings"] == space["bindings"]
    finally:
        state.space_service.delete(space["id"])


def test_expired_lease_fences_late_completion_and_restart_claims_work(graph_workspace):
    factory, upload, label = graph_workspace
    staged = stage(factory, upload, label)
    clock = [datetime.now(timezone.utc)]
    state = factory()
    first = worker(state, clock=lambda: clock[0], lease_seconds=30)
    old = first.claim(event_id(state, staged))
    assert old
    assert worker(factory(), clock=lambda: clock[0]).claim(old["id"]) is None
    clock[0] += timedelta(seconds=31)
    second = worker(factory(), clock=lambda: clock[0])
    current = second.claim(old["id"])
    assert current["lease_token"] != old["lease_token"]
    assert not first.execute(old)
    assert state.get_run(staged["run_id"])["status"] == "running"
    assert second.execute(current)
    assert state.get_run(staged["run_id"])["status"] == "succeeded"


def test_failure_retries_with_backoff_and_sanitized_terminal_error(graph_workspace):
    factory, upload, label = graph_workspace
    staged = stage(factory, upload, label)
    state = factory()
    clock = [datetime.now(timezone.utc)]
    class Broken:
        def prepare(self, manifest, heartbeat):
            raise RuntimeError("secret-provider-token")
    job = worker(state, Broken(), clock=lambda: clock[0], max_attempts=2)
    identifier = event_id(state, staged)
    assert job.run_once(identifier)
    assert not job.run_once(identifier)
    assert state.get_run(staged["run_id"])["status"] == "running"
    clock[0] += timedelta(minutes=10)
    assert job.run_once(identifier)
    run = state.get_run(staged["run_id"])
    assert run["status"] == "failed"
    assert "secret" not in str(run)
    assert state.graph_service.repository.get_record("outbox", identifier)["status"] == "failed"
    with pytest.raises(DomainConflict) as exc:
        state.graph_service.publish(upload.material.id, staged["candidate_revision_id"], {"expected_graph_version": 0}, label+"-publish")
    assert exc.value.code == "REVISION_NOT_READY"


def test_cancel_during_external_io_cannot_publish_late_success(graph_workspace):
    factory, upload, label = graph_workspace
    staged = stage(factory, upload, label)
    state = factory()
    class Cancelling(PreparedBackend):
        def prepare(self, manifest, heartbeat):
            result = super().prepare(manifest, heartbeat)
            factory().cancel_run(staged["run_id"])
            return result
    assert worker(state, Cancelling()).run_once(event_id(state, staged))
    assert state.get_run(staged["run_id"])["status"] == "cancelled"
    revision = state.graph_service.repository.get_record("graph_revisions", staged["candidate_revision_id"])
    assert revision["status"] == "draft"
    assert not revision.get("preparation")


def test_invalid_receipt_is_never_ready(graph_workspace):
    factory, upload, label = graph_workspace
    staged = stage(factory, upload, label)
    state = factory()
    class Invalid(PreparedBackend):
        def prepare(self, manifest, heartbeat):
            result = super().prepare(manifest, heartbeat)
            result["manifest_hash"] = "different"
            return result
    assert worker(state, Invalid(), max_attempts=1).run_once(event_id(state, staged))
    assert state.get_run(staged["run_id"])["status"] == "failed"
    assert state.graph_service.diff(upload.material.id)["status"] == "draft"


@pytest.mark.parametrize("action", ["keep_old", "use_new", "keep_both"])
def test_review_decisions_pin_effective_sources_and_exclude_conflicted_questions(graph_workspace, action):
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    worker(state).run_once(event_id(state, staged))
    state.graph_service.publish(upload.material.id, staged["candidate_revision_id"], {"expected_graph_version": 0}, label+"-publish1")
    previous = state.create_space({"material_ids": [upload.material.id]}, idempotency_key=label+"-oldspace")
    current = None
    try:
        newer = state.material_service.create_version(material_id=upload.material.id, filename="notes.md", content=b"# Functions\n\nChanged function semantics.", idempotency_key=label+"-v2")
        staged2 = state.graph_service.reconcile(upload.material.id, {"version_id": newer.version.id, "expected_graph_version": 1}, label+"-stage2")
        worker(state).run_once(event_id(state, staged2))
        with pytest.raises(DomainConflict) as exc:
            state.graph_service.publish(upload.material.id, staged2["candidate_revision_id"], {"expected_graph_version": 1}, label+"-publish2")
        assert exc.value.code == "GRAPH_CONFLICTS_PENDING"
        conflict = state.graph_service.diff(upload.material.id)["conflicts"][0]
        state.graph_service.publish(upload.material.id, staged2["candidate_revision_id"], {"expected_graph_version": 1,
            "resolutions": [{"conflict_id": conflict["conflict_id"], "action": action, "reason": "Reviewed sources"}]}, label+"-publish2")
        restored = factory()
        current = restored.create_space({"material_ids": [upload.material.id]}, idempotency_key=label+"-newspace")
        assert current["bindings"][0]["graph_version"] == 2
        assert current["bindings"][0]["graph_revision_id"] == staged2["candidate_revision_id"]
        old_topics = restored.space_service.bound_topics(restored.get_space(previous["id"]))
        assert {ref["material_version_id"] for t in old_topics for ref in t["source_refs"]} == {upload.version.id}
        topics = restored.space_service.bound_topics(current)
        expected = {upload.version.id} if action == "keep_old" else {newer.version.id} if action == "use_new" else {upload.version.id, newer.version.id}
        assert {ref["material_version_id"] for t in topics for ref in t["source_refs"]} == expected
        eligible = restored.assessment_service._topics(current)
        assert bool(eligible) == (action != "keep_both")
        sources = restored.message_service._sources(current)
        assert {source["material_version_id"] for source in sources} == expected
        assert {source["graph_version"] for source in sources} == {2}
    finally:
        state.space_service.delete(previous["id"])
        if current:
            state.space_service.delete(current["id"])


def test_recovery_preserves_durable_graph_jobs(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    run = state.run_service.create("export")
    # Restrict to fixture-owned IDs, never recover unrelated local database work.
    state.run_service.repository.active_ids = lambda: [run["id"], staged["run_id"]]
    assert state.run_service.recover_interrupted(exclude_kinds={"graph_reconcile"}) == 1
    assert state.get_run(staged["run_id"])["status"] == "queued"
    assert state.get_run(run["id"])["status"] == "failed"


def test_concurrent_workers_claim_only_once(graph_workspace):
    from concurrent.futures import ThreadPoolExecutor
    factory, upload, label = graph_workspace
    staged = stage(factory, upload, label)
    identifier = event_id(factory(), staged)
    def claim(_):
        return worker(factory()).claim(identifier)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert sum(result is not None for result in results) == 1


def test_publication_transaction_rolls_back_and_same_key_can_retry(graph_workspace, monkeypatch):
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    worker(state).run_once(event_id(state, staged))
    repository = state.graph_service.repository
    original = repository.remember
    def fail(*args, **kwargs):
        raise RuntimeError("injected persistence failure")
    monkeypatch.setattr(repository, "remember", fail)
    with pytest.raises(RuntimeError):
        state.graph_service.publish(upload.material.id, staged["candidate_revision_id"], {"expected_graph_version": 0}, label+"-publish")
    restored = factory()
    assert restored.graph_service.diff(upload.material.id)["status"] == "pending_review"
    monkeypatch.setattr(repository, "remember", original)
    assert state.graph_service.publish(upload.material.id, staged["candidate_revision_id"], {"expected_graph_version": 0}, label+"-publish")["graph_version"] == 1


def test_material_deletion_during_preparation_discards_receipt(graph_workspace):
    from fastapi.testclient import TestClient
    from my_agent_llms.learning.api import create_app
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    class Deleting(PreparedBackend):
        def prepare(self, manifest, heartbeat):
            receipt = super().prepare(manifest, heartbeat)
            with TestClient(create_app(service=state.material_service, run_service=state.run_service)) as client:
                assert client.delete(f"/api/v1/materials/{upload.material.id}").status_code == 202
            return receipt
    worker(state, Deleting()).run_once(event_id(state, staged))
    assert state.get_run(staged["run_id"])["status"] == "cancelled"
    assert state.graph_service.repository.records("graph_revisions", material_id=upload.material.id) == []


def test_complete_pipeline_with_real_graph_and_vector_stores(graph_workspace):
    import os
    if not os.getenv("LEARNING_TEST_NEO4J_URI") or not os.getenv("LEARNING_TEST_QDRANT_URL"):
        pytest.skip("requires explicit Neo4j and Qdrant test endpoints")
    from neo4j import GraphDatabase
    from qdrant_client import QdrantClient
    from my_agent_llms.learning.graph_preparation import GraphPreparer, Neo4jGraphBackend
    from my_agent_llms.learning.vector_retrieval import VectorRetriever, QdrantVectorBackend
    from my_agent_llms.test.test_learning_graph_preparation import Embedder
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    identifier = staged["candidate_revision_id"]
    driver = GraphDatabase.driver(os.environ["LEARNING_TEST_NEO4J_URI"], auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]))
    client = QdrantClient(url=os.environ["LEARNING_TEST_QDRANT_URL"], check_compatibility=False)
    backend = QdrantVectorBackend(client, collection_prefix=label)
    preparer = GraphPreparer(state.material_repository, Neo4jGraphBackend(driver), VectorRetriever(Embedder(), backend))
    try:
        worker(state, preparer).run_once(event_id(state, staged))
        assert state.get_run(staged["run_id"])["status"] == "succeeded"
        published = state.graph_service.publish(upload.material.id, identifier, {"expected_graph_version": 0}, label+"-publish")
        assert published["graph_version"] == 1
        assert client.count(backend.collection, exact=True).count == len(upload.version.chunks)
    finally:
        if client.collection_exists(backend.collection):
            client.delete_collection(backend.collection)
        with driver.session() as session:
            session.run("MATCH (n) WHERE n.kp_revision_id = $id OR (n:KPRevision AND n.id = $id) DETACH DELETE n", id=identifier).consume()
        preparer.close()
