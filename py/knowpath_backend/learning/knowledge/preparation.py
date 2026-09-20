"""Prepare immutable Neo4j variants and exact Qdrant source points."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

from knowpath_backend.learning.errors import DomainNotFound

from neo4j import GraphDatabase, Query

from knowpath_backend.learning.knowledge.reconciliation import digest
from knowpath_backend.learning.rag.retrieval import configured_vector_retriever


class GraphPreparer:
    def __init__(self, materials, graph, vectors):
        self.materials, self.graph, self.vectors = materials, graph, vectors

    @contextmanager
    def _writer(self, material_id):
        # The same material row lock serializes erasure with each external write.
        # A lease alone cannot fence a provider call that finishes after expiry.
        with self.materials.transaction():
            if self.materials.get_material(material_id) is None:
                raise DomainNotFound("material", material_id)
            yield

    def prepare(self, manifest, heartbeat):
        if (digest({k: v for k, v in manifest.items() if k != "manifest_hash"}) != manifest["manifest_hash"]
                or digest(manifest["snapshot"]) != manifest["snapshot_hash"]):
            raise ValueError("invalid manifest")
        versions, sources = {}, {}
        expected = {s["id"]: s for s in manifest["snapshot"]["sources"]}
        for node in manifest["snapshot"]["nodes"] + manifest["old_nodes"]:
            for ref in node["source_refs"]:
                version_id = ref["material_version_id"]
                if version_id not in versions:
                    versions[version_id] = self.materials.get_version(version_id)
                version = versions[version_id]
                if version is None or version.material_id != ref["material_id"] or version.material_id != manifest["snapshot"]["material_id"]:
                    raise ValueError("source ownership mismatch")
                chunk = next((c for c in version.chunks if c.id == ref["chunk_id"]), None)
                if chunk is None or (chunk.id in expected and chunk.content_hash != expected[chunk.id]["content_hash"]):
                    raise ValueError("source content mismatch")
                sources[chunk.id] = {**ref, "topic_id": node["id"], "topic_name": node["name"],
                    "graph_version": manifest["graph_version"], "content_hash": chunk.content_hash, "text": chunk.text}
        sources = [sources[k] for k in sorted(sources)]
        if not sources:
            raise ValueError("empty prepared graph")
        heartbeat()
        with self._writer(manifest["snapshot"]["material_id"]):
            graph_receipt = self.graph.prepare(manifest, sources)
        for start in range(0, len(sources), 10):
            heartbeat()
            with self._writer(manifest["snapshot"]["material_id"]):
                self.vectors.index(sources[start:start + 10])
        heartbeat()
        self.vectors.backend.require_index(sources)
        heartbeat()
        return {"manifest_hash": manifest["manifest_hash"], "neo4j": graph_receipt,
                "qdrant": {"verified": True, "collection": self.vectors.backend.collection, "indexed_chunks": len(sources)}}

    def close(self):
        try:
            self.graph.close()
        finally:
            self.vectors.close()


GRAPH_RELATION_TYPES = ("contains", "prerequisite_of", "related_to", "assessed_by", "explained_by", "supersedes", "contradicts")


class Neo4jGraphBackend:
    """Neo4j is rebuildable; only the SQL publication transaction grants visibility."""
    def __init__(self, driver, *, database=None):
        self.driver, self.database = driver, database
        self.initialized = False

    def close(self):
        self.driver.close()

    def prepare(self, manifest, sources):
        identifier = manifest["revision_id"]
        topics, edges = [], []
        for side, nodes in (("new", manifest["snapshot"]["nodes"]), ("old", manifest["old_nodes"])):
            for node in nodes:
                key = digest([identifier, side, node["id"]])
                topics.append({"id": key, "topic_id": node["id"], "variant": side,
                               "payload": json.dumps(node, sort_keys=True, ensure_ascii=False)})
                edges.extend({"topic": key, "source": digest([identifier, ref["chunk_id"]])} for ref in node["source_refs"])
        source_rows = [{"id": digest([identifier, s["chunk_id"]]), "payload": json.dumps(
            {k: v for k, v in s.items() if k not in {"text", "topic_name", "topic_id"}}, sort_keys=True, ensure_ascii=False)} for s in sources]
        relations = []
        node_ids = {node["id"] for node in manifest["snapshot"]["nodes"]}
        for relation in manifest["snapshot"].get("relations", []):
            if relation["type"] not in GRAPH_RELATION_TYPES:
                raise ValueError("unsupported graph relation")
            if relation["from_id"] not in node_ids or relation["to_id"] not in node_ids:
                raise ValueError("missing graph endpoint")
            relations.append({"id": digest([identifier, relation["id"]]), "kind": relation["type"],
                "source": digest([identifier, "new", relation["from_id"]]),
                "target": digest([identifier, "new", relation["to_id"]]),
                "payload": json.dumps(relation, sort_keys=True, ensure_ascii=False)})
        payload = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
        with self.driver.session(database=self.database) as session:
            if not self.initialized:
                for label in ("KPRevision", "KPTopicVariant", "KPSource"):
                    session.run(Query(f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE", timeout=60)).consume()
                self.initialized = True

            def store(tx):
                row = tx.run("MERGE (r:KPRevision {id: $id}) ON CREATE SET r.manifest_hash = $hash, r.payload = $payload "
                             "RETURN r.manifest_hash AS hash, r.payload AS payload", id=identifier, hash=manifest["manifest_hash"], payload=payload).single()
                if row["hash"] != manifest["manifest_hash"] or row["payload"] != payload:
                    raise ValueError("immutable Neo4j revision mismatch")
                tx.run("MATCH (r:KPRevision {id: $id}) UNWIND $rows AS row "
                       "MERGE (n:KPTopicVariant {id: row.id}) SET n.kp_revision_id = $id, n.topic_id = row.topic_id, "
                       "n.variant = row.variant, n.payload = row.payload MERGE (r)-[:HAS_TOPIC]->(n)", id=identifier, rows=topics).consume()
                tx.run("MATCH (r:KPRevision {id: $id}) UNWIND $rows AS row "
                       "MERGE (n:KPSource {id: row.id}) SET n.kp_revision_id = $id, n.payload = row.payload "
                       "MERGE (r)-[:HAS_SOURCE]->(n)", id=identifier, rows=source_rows).consume()
                tx.run("UNWIND $rows AS row MATCH (t:KPTopicVariant {id: row.topic}), (s:KPSource {id: row.source}) "
                       "MERGE (t)-[:SUPPORTED_BY]->(s)", rows=edges).consume()
                # Relationship type comes only from this fixed allowlist. Each
                # candidate edge remains scoped to its immutable revision.
                for kind in GRAPH_RELATION_TYPES:
                    tx.run("UNWIND $rows AS row MATCH (a:KPTopicVariant {id: row.source}), (b:KPTopicVariant {id: row.target}) "
                           f"MERGE (a)-[r:{kind} {{id: row.id}}]->(b) SET r.kp_revision_id=$id, r.payload=row.payload",
                           rows=[r for r in relations if r["kind"] == kind], id=identifier).consume()
            session.execute_write(store)

            def verify(tx):
                row = tx.run("MATCH (r:KPRevision {id: $id}) RETURN r.manifest_hash AS hash, r.payload AS payload", id=identifier).single()
                if row is None or row["hash"] != manifest["manifest_hash"] or row["payload"] != payload:
                    return False
                actual_topics = [dict(r) for r in tx.run("MATCH (:KPRevision {id: $id})-[:HAS_TOPIC]->(t) "
                    "RETURN t.id AS id, t.topic_id AS topic_id, t.variant AS variant, t.payload AS payload", id=identifier)]
                actual_sources = [dict(r) for r in tx.run("MATCH (:KPRevision {id: $id})-[:HAS_SOURCE]->(s) "
                    "RETURN s.id AS id, s.payload AS payload", id=identifier)]
                actual_edges = [dict(r) for r in tx.run("MATCH (t:KPTopicVariant {kp_revision_id: $id})-[:SUPPORTED_BY]->(s) "
                    "RETURN t.id AS topic, s.id AS source", id=identifier)]
                actual_relations = [dict(r) for r in tx.run(
                    "MATCH (a:KPTopicVariant {kp_revision_id:$id})-[r]->(b:KPTopicVariant {kp_revision_id:$id}) "
                    "WHERE type(r) IN $kinds "
                    "RETURN r.id AS id, type(r) AS kind, a.id AS source, b.id AS target, r.payload AS payload", id=identifier, kinds=list(GRAPH_RELATION_TYPES))]
                canonical = lambda rows: sorted(json.dumps(r, sort_keys=True) for r in rows)
                return canonical(actual_topics) == canonical(topics) and canonical(actual_sources) == canonical(source_rows) and canonical(actual_edges) == canonical(edges) and canonical(actual_relations) == canonical(relations)
            if not session.execute_read(verify):
                raise ValueError("Neo4j readback mismatch")
        return {"verified": True, "revision_id": identifier, "topic_variants": len(topics), "source_count": len(sources), "relation_count": len(relations)}


def configured_graph_preparer(materials):
    vectors = configured_vector_retriever()
    try:
        password = os.getenv("NEO4J_PASSWORD", "")
        if not password:
            raise ValueError("NEO4J_PASSWORD is required")
        driver = GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
            auth=(os.getenv("NEO4J_USERNAME", "neo4j"), password), connection_timeout=30,
            connection_acquisition_timeout=30, max_transaction_retry_time=30)
        return GraphPreparer(materials, Neo4jGraphBackend(driver, database=os.getenv("NEO4J_DATABASE") or None), vectors)
    except Exception:
        vectors.close()
        raise
