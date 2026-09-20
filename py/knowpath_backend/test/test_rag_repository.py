"""Immutable RAG storage and fail-closed source mappings."""

from datetime import datetime
from importlib.util import find_spec

import pytest
import sqlalchemy as sa

from knowpath_backend.learning.persistence.db import Base


@pytest.fixture
def repo():
    assert find_spec("knowpath_backend.learning.persistence.rag_repository") is not None, "RAG repository is missing"
    from knowpath_backend.learning.persistence.rag_repository import SqlRagRepository
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    sa.event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        now = datetime(2026, 9, 20)
        connection.execute(Base.metadata.tables["materials"].insert(), dict(id="material", name="Notes", type="txt", status="ready", size_bytes=20, created_at=now, updated_at=now))
        connection.execute(Base.metadata.tables["material_versions"].insert(), dict(id="mv", material_id="material", filename="notes.txt", media_type="text/plain", content_hash="a" * 64, size_bytes=20, status="ready", created_at=now))
        connection.execute(Base.metadata.tables["learning_spaces"].insert(), dict(id="space", name="Space", status="active", bindings=[], topic_ids=[], excluded_topic_ids=[], created_at=now, updated_at=now))
    yield SqlRagRepository(engine), engine
    engine.dispose()


def span(start=0, end=10, **overrides):
    return dict(material_version_id="mv", artifact_hash="a" * 64, start=start, end=end, page=1, block="p1", **overrides)


def seed(repo, retrieval_version_id="rv"):
    repo.put_retrieval_version(dict(retrieval_version_id=retrieval_version_id, material_version_id="mv", profile={"model": "embed-v1", "dimensions": 32}, profile_hash="b" * 64))
    repo.put_chunk(dict(retrieval_version_id=retrieval_version_id, chunk_id="chunk", material_version_id="mv", source_text="0123456789"), [span()])
    repo.put_scope_snapshot(dict(scope_snapshot_id="scope", space_id="space", scope_version=1, bindings=[dict(material_id="material", material_version_id="mv", graph_version=2)]), [span(0, 20)])


def test_immutable_replay_and_defensive_reads(repo):
    store, _ = repo
    from knowpath_backend.learning.persistence.rag_repository import RagConflict
    seed(store)
    seed(store)
    value = store.get_retrieval_version("rv")
    value["profile"]["model"] = "changed"
    assert store.get_retrieval_version("rv")["profile"]["model"] == "embed-v1"
    with pytest.raises(RagConflict):
        store.put_retrieval_version(dict(retrieval_version_id="rv", material_version_id="mv", profile={"model": "changed"}, profile_hash="b" * 64))
    with pytest.raises(RagConflict):
        store.put_chunk(dict(retrieval_version_id="rv", chunk_id="chunk", material_version_id="mv", source_text="overwrite"), [span()])
    with pytest.raises(RagConflict):
        store.put_scope_snapshot(dict(scope_snapshot_id="scope", space_id="space", scope_version=1, bindings=[dict(material_id="material", material_version_id="mv", graph_version=2)]), [span(0, 19)])
    assert store.get_scope_snapshot("scope")["allowed_spans"] == [span(0, 20)]


def test_mapping_is_version_scoped_and_requires_containment(repo):
    store, _ = repo
    from knowpath_backend.learning.persistence.rag_repository import RagIntegrityError
    seed(store)
    seed(store, "rv2")
    mapping = dict(scope_snapshot_id="scope", retrieval_version_id="rv", chunk_id="chunk")
    store.put_scope_chunk_map(mapping)
    store.put_scope_chunk_map(mapping)
    assert [item["chunk_id"] for item in store.list_scope_chunks("scope", "rv")] == ["chunk"]
    assert store.list_scope_chunks("scope", "rv2") == []
    store.put_chunk(dict(retrieval_version_id="rv", chunk_id="outside", material_version_id="mv", source_text="outside"), [span(19, 25)])
    for bad in [dict(mapping, retrieval_version_id="missing"), dict(mapping, chunk_id="outside"), dict(mapping, scope_snapshot_id="missing")]:
        with pytest.raises(RagIntegrityError):
            store.put_scope_chunk_map(bad)
    assert len(store.list_scope_chunks("scope", "rv")) == 1


def test_source_identity_and_empty_or_invalid_spans_are_rejected(repo):
    store, _ = repo
    from knowpath_backend.learning.persistence.rag_repository import RagIntegrityError
    seed(store)
    chunk = dict(retrieval_version_id="rv", chunk_id="bad", material_version_id="mv", source_text="x")
    for spans in [[], [span(10, 10)], [span(-1, 3)], [dict(span(), material_version_id="missing")]]:
        with pytest.raises(RagIntegrityError):
            store.put_chunk(chunk, spans)
    assert store.get_chunk("rv", "bad") is None
    with pytest.raises(RagIntegrityError):
        store.put_scope_snapshot(dict(scope_snapshot_id="bad", space_id="space", scope_version=1, bindings=[dict(material_id="another", material_version_id="mv", graph_version=2)]), [span()])
    assert store.get_scope_snapshot("bad") is None


def test_mapping_compares_every_span_identity_component(repo):
    store, _ = repo
    from knowpath_backend.learning.persistence.rag_repository import RagIntegrityError
    seed(store)
    for index, override in enumerate([{"artifact_hash": "c" * 64}, {"page": 2}, {"block": "p2"}]):
        chunk_id = f"other-{index}"
        store.put_chunk(dict(retrieval_version_id="rv", chunk_id=chunk_id, material_version_id="mv", source_text="other"), [dict(span(), **override)])
        with pytest.raises(RagIntegrityError):
            store.put_scope_chunk_map(dict(scope_snapshot_id="scope", retrieval_version_id="rv", chunk_id=chunk_id))


def test_composite_foreign_keys_reject_cross_version_nodes_and_maps(repo):
    store, engine = repo
    seed(store)
    store.put_node(dict(retrieval_version_id="rv", node_id="parent", parent_node_id=None, chunk_id="chunk", kind="leaf"))
    assert store.get_node("rv", "parent")["chunk_id"] == "chunk"
    store.put_retrieval_version(dict(retrieval_version_id="rv2", material_version_id="mv", profile={}, profile_hash="c" * 64))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(Base.metadata.tables["rag_scope_chunk_map"].insert(), dict(scope_snapshot_id="scope", retrieval_version_id="rv2", chunk_id="chunk"))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(Base.metadata.tables["rag_nodes"].insert(), dict(retrieval_version_id="rv2", node_id="child", parent_node_id="parent", chunk_id=None, kind="section", payload={}))


def test_chunk_and_spans_roll_back_together_on_database_failure(repo):
    store, engine = repo
    seed(store)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER reject_span BEFORE INSERT ON rag_chunk_spans BEGIN SELECT RAISE(ABORT, 'injected span failure'); END")
    with pytest.raises(sa.exc.IntegrityError):
        store.put_chunk(dict(retrieval_version_id="rv", chunk_id="failed", material_version_id="mv", source_text="01234"), [span(0, 5)])
    assert store.get_chunk("rv", "failed") is None


def test_reads_reject_out_of_band_mapping_outside_scope(repo):
    store, engine = repo
    from knowpath_backend.learning.persistence.rag_repository import RagIntegrityError
    seed(store)
    store.put_chunk(dict(retrieval_version_id="rv", chunk_id="outside", material_version_id="mv", source_text="outside"), [span(19, 25)])
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["rag_scope_chunk_map"].insert(), dict(scope_snapshot_id="scope", retrieval_version_id="rv", chunk_id="outside"))
    with pytest.raises(RagIntegrityError):
        store.list_scope_chunks("scope", "rv")


def test_mapping_accepts_union_of_adjacent_spans_but_not_gaps(repo):
    store, _ = repo
    from knowpath_backend.learning.persistence.rag_repository import RagIntegrityError
    seed(store)
    base = dict(space_id="space", scope_version=2, bindings=[dict(material_id="material", material_version_id="mv", graph_version=2)])
    store.put_scope_snapshot(dict(base, scope_snapshot_id="adjacent"), [span(0, 5), span(5, 10)])
    store.put_scope_chunk_map(dict(scope_snapshot_id="adjacent", retrieval_version_id="rv", chunk_id="chunk"))
    assert len(store.list_scope_chunks("adjacent", "rv")) == 1
    store.put_scope_snapshot(dict(base, scope_snapshot_id="gap"), [span(0, 4), span(5, 10)])
    with pytest.raises(RagIntegrityError):
        store.put_scope_chunk_map(dict(scope_snapshot_id="gap", retrieval_version_id="rv", chunk_id="chunk"))


def test_retrieval_version_binds_one_material_version(repo):
    store, engine = repo
    from knowpath_backend.learning.persistence.rag_repository import RagIntegrityError
    seed(store)
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["material_versions"].insert(), dict(id="mv2", material_id="material", filename="notes.txt", media_type="text/plain", content_hash="b" * 64, size_bytes=20, status="ready", created_at=datetime(2026, 9, 20)))
    with pytest.raises(RagIntegrityError):
        store.put_chunk(dict(retrieval_version_id="rv", chunk_id="wrong-material", material_version_id="mv2", source_text="x"), [dict(span(), material_version_id="mv2")])


def test_invalid_hash_and_zero_graph_version_are_rejected(repo):
    store, _ = repo
    from knowpath_backend.learning.persistence.rag_repository import RagIntegrityError
    seed(store)
    with pytest.raises(RagIntegrityError):
        store.put_chunk(dict(retrieval_version_id="rv", chunk_id="bad", material_version_id="mv", source_text="x"), [dict(span(), artifact_hash="z" * 64)])
    with pytest.raises(RagIntegrityError):
        store.put_scope_snapshot(dict(scope_snapshot_id="bad", space_id="space", scope_version=1, bindings=[dict(material_id="material", material_version_id="mv", graph_version=0)]), [span()])
