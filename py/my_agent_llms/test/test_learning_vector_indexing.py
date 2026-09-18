"""Explicit material index preparation never changes authoritative version pointers."""
import importlib
import pytest
from my_agent_llms.learning.materials import MaterialService, InMemoryMaterialRepository
from my_agent_llms.learning.vector_retrieval import RetrievalError


class Indexer:
    def __init__(self):
        self.rows = None
    def index(self, rows):
        self.rows = rows
        return {"collection": "test", "indexed_chunks": len(rows)}


def prepared():
    repo = InMemoryMaterialRepository()
    service = MaterialService(repo)
    result = service.create(filename="notes.md", content=b"# Functions\nFunctions group reusable behavior.\n\n# Loops\nLoops repeat work.",
                            idempotency_key="indexing-test")
    return repo, service, result


def test_index_reads_exact_version_and_keeps_material_pointer_unchanged():
    module = importlib.import_module("my_agent_llms.learning.vector_indexing")
    repo, service, old = prepared()
    new = service.create_version(material_id=old.material.id, filename="new.md", content=b"# Changed\nOther material.",
                                 idempotency_key="new-version")
    indexer = Indexer()
    result = module.index_material_version(repo, indexer, old.version.id)
    assert result["indexed_chunks"] == len(old.version.chunks)
    assert {s["chunk_id"] for s in indexer.rows} == {c.id for c in old.version.chunks}
    assert all(s["graph_version"] == 1 and s["material_version_id"] == old.version.id for s in indexer.rows)
    assert repo.get_material(old.material.id).current_version_id == new.version.id


def test_missing_or_unready_version_does_not_call_indexer():
    module = importlib.import_module("my_agent_llms.learning.vector_indexing")
    repo, service, result = prepared()
    indexer = Indexer()
    with pytest.raises(RetrievalError, match="MATERIAL_VERSION_UNAVAILABLE"):
        module.index_material_version(repo, indexer, "missing")
    assert indexer.rows is None
    repo.versions[result.version.id].status = "pending"
    with pytest.raises(RetrievalError, match="MATERIAL_VERSION_NOT_READY"):
        module.index_material_version(repo, indexer, result.version.id)
    assert indexer.rows is None


def test_builder_matches_message_snapshot_identity():
    from my_agent_llms.learning.state import LearningState
    from my_agent_llms.learning.vector_retrieval import point_id
    module = importlib.import_module("my_agent_llms.learning.vector_indexing")
    repo, service, result = prepared()
    state = LearningState(repo)
    space = state.create_space({"name": "Study", "material_ids": [result.material.id]})
    topics = state.space_service.bound_topics(space)
    state.set_scope(space["id"], {"expected_version": 1, "topic_ids": [t["id"] for t in topics]})
    indexer = Indexer()
    module.index_material_version(repo, indexer, result.version.id)
    candidates = state.message_service._sources(state.get_space(space["id"]))
    assert {point_id(s) for s in candidates} == {point_id(s) for s in indexer.rows}


def test_cli_reports_sanitized_failure_and_closes_resources(monkeypatch, capsys):
    module = importlib.import_module("my_agent_llms.learning.vector_indexing")
    closed = []
    class Engine:
        def dispose(self):
            closed.append("engine")
    class BrokenIndexer:
        def close(self):
            closed.append("indexer")
    monkeypatch.setattr(module, "create_db_engine", Engine)
    monkeypatch.setattr(module, "configured_vector_retriever", BrokenIndexer)
    monkeypatch.setattr(module, "SqlAlchemyMaterialRepository", lambda engine: object())
    def fail(*args):
        raise RuntimeError("database-secret")
    monkeypatch.setattr(module, "index_material_version", fail)
    assert module.main(["--version-id", "version-1"]) == 1
    output = capsys.readouterr()
    assert "database-secret" not in output.out + output.err
    assert "VECTOR_INDEX_BUILD_FAILED" in output.err
    assert set(closed) == {"engine", "indexer"}
