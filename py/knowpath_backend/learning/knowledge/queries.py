"""Versioned, immutable graph projections read from durable snapshots."""
from copy import deepcopy
from knowpath_backend.learning.errors import DomainNotFound
from knowpath_backend.learning.knowledge.reconciliation import extract_snapshot
from knowpath_backend.learning.knowledge.relations import apply_prerequisites


class GraphQueryService:
    def __init__(self, graphs):
        self.graphs = graphs

    def _snapshot(self, material_id, version_id=None):
        material = self.graphs._material(material_id)
        history = self.graphs._history(material_id)
        published = [r for r in history if r["status"] in {"published", "superseded"} and r.get("publication")
                     and (version_id is None or r["material_version_id"] == version_id)]
        if published:
            revision = max(published, key=lambda r: r["graph_version"])
            snapshot = deepcopy(revision["publication"]["snapshot"])
            metadata = {"revision_id": revision["id"], "graph_version": revision["graph_version"],
                        "material_version_id": revision["material_version_id"],
                        "valid_from": revision["publication"].get("published_at"),
                        "valid_to": next((r["publication"].get("published_at") for r in sorted(history, key=lambda r: r.get("graph_version") or 0)
                                          if r.get("publication") and (r.get("graph_version") or 0) > revision["graph_version"]), None),
                        "recorded_at": revision.get("created_at")}
        else:
            version = self.graphs.materials.get_version(version_id or material.current_version_id)
            if version is None or version.material_id != material_id:
                raise DomainNotFound("material_version", version_id)
            snapshot = extract_snapshot(material_id, version)
            metadata = {"revision_id": None, "graph_version": 0, "material_version_id": version.id,
                        "valid_from": None, "valid_to": None, "recorded_at": version.created_at.isoformat()}
        apply_prerequisites(snapshot)
        for record in snapshot["nodes"] + snapshot["relations"]:
            record.update(metadata)
        return snapshot, metadata

    def material_topics(self, material_id, version_id=None, *, include_inactive=False, depth=None):
        snapshot, metadata = self._snapshot(material_id, version_id)
        nodes = [node for node in snapshot["nodes"] if (include_inactive or node.get("status") == "active")
                 and (depth is None or node.get("level", 1) <= depth)]
        return {"material_id": material_id, "version_id": metadata["material_version_id"],
                "graph_version": metadata["graph_version"], "items": nodes}

    def _snapshots(self):
        for material in self.graphs.materials.list_materials():
            yield self._snapshot(material.id)
            for version in reversed(self.graphs.materials.list_versions(material.id)):
                yield self._snapshot(material.id, version.id)

    def topic_graph(self, topic_id, *, depth=1, include_sources=True):
        for snapshot, metadata in self._snapshots():
            nodes = {n["id"]: n for n in snapshot["nodes"]}
            if topic_id not in nodes:
                continue
            edges = [e for e in snapshot["relations"] if e.get("from_id") in nodes and e.get("to_id") in nodes]
            selected, frontier = {topic_id}, {topic_id}
            for _ in range(depth):
                neighbors = {identifier for edge in edges if edge["from_id"] in frontier or edge["to_id"] in frontier
                             for identifier in (edge["from_id"], edge["to_id"])}
                frontier = neighbors - selected
                selected.update(neighbors)
            result = {"nodes": [n for n in snapshot["nodes"] if n["id"] in selected],
                      "edges": [e for e in edges if e["from_id"] in selected and e["to_id"] in selected],
                      "graph_version": metadata["graph_version"]}
            if include_sources:
                refs = {r["chunk_id"] for item in result["nodes"] + result["edges"] for r in item.get("source_refs", [])}
                result["sources"] = [source for source in snapshot["sources"] if source["id"] in refs]
            else:
                for record in result["nodes"] + result["edges"]:
                    record.pop("source_refs", None)
                    record.pop("evidence", None)
            return result
        raise DomainNotFound("topic", topic_id)
