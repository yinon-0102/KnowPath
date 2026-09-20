"""The additive schema must not prevent existing erasure commands."""
import pytest
import sqlalchemy as sa

from knowpath_backend.test.test_rag_repository import repo, seed
from knowpath_backend.learning.persistence.db import Base
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.persistence.space_repository import SqlAlchemySpaceRepository


def count(engine, name):
    with engine.connect() as connection:
        return connection.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[name]))


def test_material_delete_removes_derivatives_and_invalidates_scope(repo):
    store, engine = repo
    seed(store)
    store.put_scope_chunk_map(dict(scope_snapshot_id="scope", retrieval_version_id="rv", chunk_id="chunk"))
    store.put_node(dict(retrieval_version_id="rv", node_id="root", kind="section"))
    store.put_node(dict(retrieval_version_id="rv", node_id="child", parent_node_id="root", kind="leaf", chunk_id="chunk"))
    SqlAlchemyMaterialRepository(engine).delete_material("material")
    assert count(engine, "materials") == 0
    assert count(engine, "rag_chunks") == 0
    assert count(engine, "rag_retrieval_versions") == 0
    assert count(engine, "rag_scope_snapshots") == 0
    assert count(engine, "learning_spaces") == 1


def test_space_delete_keeps_shared_content_index(repo):
    store, engine = repo
    seed(store)
    store.put_scope_chunk_map(dict(scope_snapshot_id="scope", retrieval_version_id="rv", chunk_id="chunk"))
    materials = SqlAlchemyMaterialRepository(engine)
    SqlAlchemySpaceRepository(materials.unit_of_work).delete("space")
    assert count(engine, "learning_spaces") == 0
    assert count(engine, "rag_scope_snapshots") == 0
    assert count(engine, "rag_scope_spans") == 0
    assert count(engine, "rag_scope_chunk_map") == 0
    assert count(engine, "rag_chunks") == 1


def test_failed_erasure_rolls_back_derivative_cleanup(repo):
    store, engine = repo
    seed(store)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER fail_erasure BEFORE DELETE ON materials BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sa.exc.IntegrityError):
        SqlAlchemyMaterialRepository(engine).delete_material("material")
    assert count(engine, "materials") == 1
    assert count(engine, "rag_chunks") == 1
    assert count(engine, "rag_scope_snapshots") == 1
