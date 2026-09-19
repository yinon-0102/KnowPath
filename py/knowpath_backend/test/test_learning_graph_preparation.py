"""Real vector adapter integration; Neo4j integration is opt-in."""
import os
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.rag.retrieval import QdrantVectorBackend, VectorRetriever


class Embedder:
    def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]


class GraphBackend:
    def prepare(self, manifest, sources):
        self.manifest, self.sources = manifest, sources
        return {"verified": True}


def test_prepare_writes_and_checks_vectors_and_both_conflict_sides():
    from knowpath_backend.learning.knowledge.preparation import GraphPreparer
    from knowpath_backend.learning.workers.graph import preparation_manifest
    from knowpath_backend.learning.knowledge.reconciliation import digest
    state = LearningState()
    old = state.material_service.create(filename="old.md", content=b"# Topic\n\nOld source.", idempotency_key="old")
    newer = state.material_service.create_version(material_id=old.material.id, filename="new.md", content=b"# Topic\n\nNew source.", idempotency_key="new")
    staged = state.graph_service.reconcile(old.material.id, {"version_id": newer.version.id, "expected_graph_version": 0}, "stage")
    revision = state.graph_service.repository.get_record("graph_revisions", staged["candidate_revision_id"])
    from knowpath_backend.learning.spaces.service import topics_for_version
    old_node = topics_for_version(old.material.id, old.version)[0]
    revision["diff"]["conflicts"] = [{"before": old_node}]
    manifest = preparation_manifest(revision)
    graph = GraphBackend()
    client = QdrantClient(":memory:")
    vectors = VectorRetriever(Embedder(), QdrantVectorBackend(client))
    heartbeats = []
    try:
        prepared = GraphPreparer(state.material_repository, graph, vectors).prepare(manifest, lambda: heartbeats.append(True))
        assert prepared["manifest_hash"] == manifest["manifest_hash"]
        assert prepared["qdrant"]["verified"] is True
        assert prepared["qdrant"]["indexed_chunks"] == 2
        assert {s["material_version_id"] for s in graph.sources} == {old.version.id, newer.version.id}
        vectors.backend.require_index(graph.sources)
        assert len(heartbeats) >= 3
        # Corruption must be rejected before external writes.
        manifest["snapshot"]["sources"][0]["content_hash"] = "wrong"
        manifest["snapshot_hash"] = digest(manifest["snapshot"])
        manifest["manifest_hash"] = digest({k: v for k, v in manifest.items() if k != "manifest_hash"})
        with pytest.raises(ValueError):
            GraphPreparer(state.material_repository, graph, vectors).prepare(manifest, lambda: None)
    finally:
        client.close()


def test_neo4j_preparation_is_idempotent_and_readback_verifies_sources():
    if not os.getenv("LEARNING_TEST_NEO4J_URI"):
        pytest.skip("requires explicit Neo4j test URI")
    from neo4j import GraphDatabase
    from knowpath_backend.learning.knowledge.preparation import Neo4jGraphBackend
    from knowpath_backend.learning.workers.graph import preparation_manifest
    from knowpath_backend.learning.knowledge.reconciliation import digest
    identifier = str(uuid4())
    snapshot = {"nodes": [{"id": "topic", "name": "Topic", "source_refs": [{"chunk_id": "chunk"}]}], "relations": []}
    manifest = preparation_manifest({"id": identifier, "snapshot": snapshot, "snapshot_hash": digest(snapshot), "base_graph_version": 0, "diff": {"conflicts": []}})
    sources = [{"chunk_id": "chunk", "material_id": identifier, "material_version_id": identifier, "text": "Safe test source", "graph_version": 1, "topic_id": "topic"}]
    driver = GraphDatabase.driver(os.environ["LEARNING_TEST_NEO4J_URI"], auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.environ["NEO4J_PASSWORD"]))
    try:
        backend = Neo4jGraphBackend(driver)
        assert backend.prepare(manifest, sources)["verified"]
        assert backend.prepare(manifest, sources)["verified"]
        with driver.session() as session:
            assert session.run("MATCH (n:KPRevision {id: $id}) RETURN count(n) AS total", id=identifier).single()["total"] == 1
    finally:
        with driver.session() as session:
            session.run("MATCH (n) WHERE n.kp_revision_id = $id OR (n:KPRevision AND n.id = $id) DETACH DELETE n", id=identifier).consume()
        driver.close()


def test_neo4j_stores_versioned_relations_and_source_provenance():
    if not os.getenv("LEARNING_TEST_NEO4J_URI"):
        pytest.skip("requires explicit Neo4j test URI")
    from neo4j import GraphDatabase
    from knowpath_backend.learning.knowledge.preparation import Neo4jGraphBackend
    from knowpath_backend.learning.workers.graph import preparation_manifest
    from knowpath_backend.learning.knowledge.reconciliation import digest
    identifier = str(uuid4())
    nodes = [{"id": name, "name": name, "source_refs": [{"chunk_id":"chunk"}]} for name in ("first","second")]
    relations = [{"id": kind, "type":kind,"from_id":"first","to_id":"second","status":"pending",
                  "source_refs":[{"chunk_id":"chunk"}]} for kind in ("contains","prerequisite_of","related_to","assessed_by","explained_by","supersedes","contradicts")]
    snapshot = {"nodes":nodes,"relations":relations}
    manifest = preparation_manifest({"id":identifier,"snapshot":snapshot,"snapshot_hash":digest(snapshot),"base_graph_version":0,"diff":{"conflicts":[]}})
    sources = [{"chunk_id":"chunk","material_id":identifier,"material_version_id":identifier,"text":"source","graph_version":1,"topic_id":"first"}]
    driver = GraphDatabase.driver(os.environ["LEARNING_TEST_NEO4J_URI"], auth=(os.getenv("NEO4J_USERNAME","neo4j"),os.environ["NEO4J_PASSWORD"]))
    try:
        backend = Neo4jGraphBackend(driver)
        assert backend.prepare(manifest,sources)["verified"]
        assert backend.prepare(manifest,sources)["verified"]
        with driver.session() as session:
            rows = list(session.run("MATCH (a:KPTopicVariant {kp_revision_id:$id})-[r]->(b:KPTopicVariant {kp_revision_id:$id}) RETURN type(r) AS kind, r.payload AS payload, a.variant AS variant",id=identifier))
            assert {r["kind"] for r in rows} == {"contains","prerequisite_of","related_to","assessed_by","explained_by","supersedes","contradicts"}
            assert len(rows) == 7
            import json
            assert all(json.loads(row["payload"])["source_refs"] == [{"chunk_id":"chunk"}] and row["variant"] == "new" for row in rows)
    finally:
        with driver.session() as session:
            session.run("MATCH (n) WHERE n.kp_revision_id=$id OR (n:KPRevision AND n.id=$id) DETACH DELETE n",id=identifier).consume()
        driver.close()
