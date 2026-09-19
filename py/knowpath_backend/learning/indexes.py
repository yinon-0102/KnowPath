"""Infrastructure ports for Neo4j graph snapshots and Qdrant chunk vectors."""

from __future__ import annotations

from typing import Any, Iterable

from .storage import GraphSettings, VectorSettings


class Neo4jGraphStore:
    def __init__(self, settings: GraphSettings | None = None) -> None:
        from neo4j import GraphDatabase

        self.settings = settings or GraphSettings.from_env()
        self.driver = GraphDatabase.driver(self.settings.uri, auth=(self.settings.username, self.settings.password))

    def close(self) -> None:
        self.driver.close()

    def upsert_snapshot(self, *, graph_version: int, nodes: Iterable[dict[str, Any]], edges: Iterable[dict[str, Any]]) -> None:
        with self.driver.session() as session:
            for node in nodes:
                session.run(
                    "MERGE (n:Topic {id: $id}) SET n += $properties, n.graph_version = $graph_version",
                    id=node["id"], properties={key: value for key, value in node.items() if key != "id"}, graph_version=graph_version,
                ).consume()
            for edge in edges:
                session.run(
                    "MATCH (a:Topic {id: $from_id}), (b:Topic {id: $to_id}) MERGE (a)-[r:KNOWLEDGE {id: $id}]->(b) SET r.type = $type, r.status = $status, r.graph_version = $graph_version",
                    from_id=edge["from_id"], to_id=edge["to_id"], id=edge["id"], type=edge["type"], status=edge.get("status", "active"), graph_version=graph_version,
                ).consume()


class QdrantVectorStore:
    def __init__(self, settings: VectorSettings | None = None) -> None:
        from qdrant_client import QdrantClient

        self.settings = settings or VectorSettings.from_env()
        self.client = QdrantClient(url=self.settings.url)

    def upsert_chunks(self, points: Iterable[Any]) -> None:
        self.client.upsert(collection_name=self.settings.collection, points=list(points), wait=True)

    def search(self, vector: list[float], *, limit: int = 5, query_filter: Any | None = None) -> list[Any]:
        return self.client.search(collection_name=self.settings.collection, query_vector=vector, limit=limit, query_filter=query_filter)
