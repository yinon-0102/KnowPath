"""Publication is transactional, scoped and backed by verified channel identities."""
from copy import deepcopy
from hashlib import sha256
from importlib.util import find_spec

import pytest
import sqlalchemy as sa

from knowpath_backend.learning.persistence.db import Base
from knowpath_backend.learning.persistence.rag_repository import RagConflict, RagIntegrityError
from knowpath_backend.test.test_rag_repository import repo, seed


@pytest.fixture
def lifecycle(repo):
    assert find_spec("knowpath_backend.learning.rag.lifecycle") is not None, "manifest lifecycle missing"
    from knowpath_backend.learning.rag.lifecycle import ManifestLifecycle
    store, engine = repo
    seed(store)
    store.put_scope_chunk_map(dict(scope_snapshot_id="scope", retrieval_version_id="rv", chunk_id="chunk"))
    scope = store.get_scope_snapshot("scope")
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["learning_spaces"].update().values(
            scope_version=1, bindings=[dict(scope["bindings"][0], graph_status="ready")]))
    return ManifestLifecycle(store), store, engine, scope


def expected():
    return [dict(chunk_id="chunk", content_hash=sha256(b"0123456789").hexdigest())]


def receipt(manifest, chunks):
    identities = [dict(chunk_id=c["chunk_id"], content_hash=sha256(c.get("retrieval_text", c["source_text"]).encode()).hexdigest()) for c in chunks]
    return {name: dict(verified=True, chunks=identities) for name in ("dense", "lexical")}


def start(lifecycle, name="manifest", **kwargs):
    manager, _, _, scope = lifecycle
    return manager.start("rv", name, scope, expected(), {"embedding_model": "v1"}, **kwargs)


def ready(lifecycle, name="manifest", **kwargs):
    manifest = start(lifecycle, name, **kwargs)
    return lifecycle[0].validate("rv", name, receipt, attempt=manifest["attempt"])


def test_a_ready_without_tree_and_immutable_exact_retry(lifecycle):
    manager, store, _, scope = lifecycle
    manifest = ready(lifecycle)
    assert manifest["status"] == "ready"
    assert manifest["a_ready"] is True and manifest["b1_ready"] is False
    assert start(lifecycle) == manifest
    with pytest.raises(RagConflict):
        manager.start("rv", "manifest", scope, expected(), {"embedding_model": "v2"})
    assert manager.publish("rv", "manifest", 0) == 1
    assert [m["manifest_id"] for m in manager.pin(scope)] == ["manifest"]
    with pytest.raises(RagIntegrityError):
        manager.pin(scope, require_b1=True)
    assert store.get_manifest("rv", "manifest")["status"] == "ready"


def test_counts_or_wrong_hash_cannot_claim_readiness(lifecycle):
    manager, store, _, _ = lifecycle
    for name, validator in [
        ("counts", lambda m, c: {"dense": {"verified": True, "count": 1}, "lexical": {"verified": True, "count": 1}}),
        ("wrong", lambda m, c: {n: {"verified": True, "chunks": [dict(chunk_id="chunk", content_hash="f" * 64)]} for n in ("dense", "lexical")}),
    ]:
        manifest = start(lifecycle, name)
        with pytest.raises(RagIntegrityError):
            manager.validate("rv", name, validator, attempt=manifest["attempt"])
        assert store.get_manifest("rv", name)["status"] == "failed"


def test_failed_build_retains_pointer_and_retry_rejects_old_attempt(lifecycle):
    manager, store, _, scope = lifecycle
    ready(lifecycle, "old")
    manager.publish("rv", "old", 0)
    failed = start(lifecycle, "new")
    manager.mark_failed("rv", "new", attempt=failed["attempt"], reason="interrupted")
    retried = start(lifecycle, "new", retry=True)
    assert retried["attempt"] != failed["attempt"]
    with pytest.raises(RagConflict):
        manager.validate("rv", "new", receipt, attempt=failed["attempt"])
    assert manager.pin(scope)[0]["manifest_id"] == "old"
    manager.validate("rv", "new", receipt, attempt=retried["attempt"])
    assert manager.publish("rv", "new", 1) == 2
    with pytest.raises(RagConflict):
        manager.publish("rv", "old", 1)
    assert manager.publish("rv", "old", 2) == 3


@pytest.mark.parametrize("change", ["scope", "bindings", "archive", "version"])
def test_revocation_blocks_publish_pin_and_rollback(lifecycle, change):
    manager, _, engine, scope = lifecycle
    ready(lifecycle, "old")
    manager.publish("rv", "old", 0)
    ready(lifecycle, "new")
    with engine.begin() as connection:
        if change == "scope":
            connection.execute(Base.metadata.tables["learning_spaces"].update().values(scope_version=2))
        elif change == "bindings":
            connection.execute(Base.metadata.tables["learning_spaces"].update().values(bindings=[]))
        elif change == "archive":
            connection.execute(Base.metadata.tables["materials"].update().values(status="archived"))
        else:
            connection.execute(Base.metadata.tables["material_versions"].update().values(status="failed"))
    for name in ("old", "new"):
        with pytest.raises(RagIntegrityError):
            manager.publish("rv", name, 1)
    with pytest.raises(RagIntegrityError):
        manager.pin(scope)


def test_scope_change_during_external_validation_fails_attempt(lifecycle):
    manager, store, engine, _ = lifecycle
    manifest = start(lifecycle)
    def validator(m, chunks):
        with engine.begin() as connection:
            connection.execute(Base.metadata.tables["learning_spaces"].update().values(scope_version=2))
        return receipt(m, chunks)
    with pytest.raises(RagIntegrityError):
        manager.validate("rv", "manifest", validator, attempt=manifest["attempt"])
    assert store.get_manifest("rv", "manifest")["status"] == "failed"


def test_sql_identity_is_rechecked_after_external_validation(lifecycle):
    manager, store, engine, _ = lifecycle
    manifest = start(lifecycle)
    def validator(m, chunks):
        result = receipt(m, chunks)
        changed = deepcopy(chunks[0])
        changed["retrieval_text"] = "tampered"
        with engine.begin() as connection:
            connection.execute(Base.metadata.tables["rag_chunks"].update().values(payload=changed))
        return result
    with pytest.raises(RagIntegrityError):
        manager.validate("rv", "manifest", validator, attempt=manifest["attempt"])
    assert store.get_manifest("rv", "manifest")["status"] == "failed"


def test_b1_requires_valid_sql_tree_not_external_claim_alone(lifecycle):
    manager, store, _, _ = lifecycle
    def tree_receipt(m, chunks):
        return dict(receipt(m, chunks), tree={"verified": True, "tree_version_id": "tree"})
    manifest = start(lifecycle, tree_version_id="tree")
    value = manager.validate("rv", "manifest", tree_receipt, attempt=manifest["attempt"])
    assert value["a_ready"] and not value["b1_ready"]
    store.put_node(dict(retrieval_version_id="rv", node_id="root", kind="root"))
    store.put_node(dict(retrieval_version_id="rv", node_id="leaf", parent_node_id="root", chunk_id="chunk", kind="leaf"))
    manifest = start(lifecycle, "tree-manifest", tree_version_id="tree")
    value = manager.validate("rv", "tree-manifest", tree_receipt, attempt=manifest["attempt"])
    assert value["a_ready"] and value["b1_ready"]


def test_duplicate_validation_does_not_cancel_running_attempt(lifecycle):
    manager, store, _, _ = lifecycle
    manifest = start(lifecycle)
    def validator(m, chunks):
        with pytest.raises(RagConflict):
            manager.validate("rv", "manifest", receipt, attempt=manifest["attempt"])
        assert store.get_manifest("rv", "manifest")["status"] == "validating"
        return receipt(m, chunks)
    assert manager.validate("rv", "manifest", validator, attempt=manifest["attempt"])["status"] == "ready"


def test_concurrent_cas_has_exactly_one_winner(lifecycle, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from knowpath_backend.learning.persistence.rag_repository import SqlRagRepository
    from knowpath_backend.learning.rag.lifecycle import ManifestLifecycle
    manager, _, source, scope = lifecycle
    ready(lifecycle, "one")
    ready(lifecycle, "two")
    target = sa.create_engine("sqlite+pysqlite:///" + str(tmp_path / "publication.db"))
    Base.metadata.create_all(target)
    with source.connect() as source_connection, target.begin() as target_connection:
        for table in Base.metadata.sorted_tables:
            values = [dict(row) for row in source_connection.execute(sa.select(table)).mappings()]
            if values:
                target_connection.execute(table.insert(), values)
    shared = ManifestLifecycle(SqlRagRepository(target))
    barrier = Barrier(2)
    def publish(name):
        barrier.wait(timeout=5)
        try:
            return shared.publish("rv", name, 0)
        except RagConflict:
            return "conflict"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(publish, ["one", "two"]))
        assert sorted(map(str, outcomes)) == ["1", "conflict"]
        assert len(shared.pin(scope)) == 1
    finally:
        target.dispose()


def test_deleted_source_cannot_be_restored_through_publish(lifecycle):
    manager, _, engine, scope = lifecycle
    ready(lifecycle)
    manager.publish("rv", "manifest", 0)
    # Simulate out-of-band corruption; normal deletion also removes RAG rows.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(Base.metadata.tables["materials"].delete())
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with pytest.raises(RagIntegrityError):
        manager.publish("rv", "manifest", 1)
    with pytest.raises(RagIntegrityError):
        manager.pin(scope)


def test_scope_mapping_requires_complete_contained_subset(lifecycle):
    manager, store, engine, _ = lifecycle
    manifest = start(lifecycle)
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["rag_scope_chunk_map"].delete())
    with pytest.raises(RagIntegrityError):
        manager.validate("rv", "manifest", receipt, attempt=manifest["attempt"])
    assert store.get_manifest("rv", "manifest")["status"] == "failed"


def test_pin_rejects_incomplete_bound_versions_and_mixed_scope_snapshots(lifecycle):
    from datetime import datetime
    manager, store, engine, scope = lifecycle
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["material_versions"].insert(), dict(
            id="mv2", material_id="material", filename="v2", media_type="text/plain",
            content_hash="b" * 64, size_bytes=10, status="ready", created_at=datetime(2026, 9, 20)))
    scope["bindings"].append(dict(material_id="material", material_version_id="mv2", graph_version=2))
    scope["scope_snapshot_id"] = "both"
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["learning_spaces"].update().values(bindings=scope["bindings"]))
    store.put_scope_snapshot(scope, scope["allowed_spans"])
    second_scope = dict(scope, scope_snapshot_id="alternate")
    store.put_scope_snapshot(second_scope, scope["allowed_spans"])
    for identifier in ("both", "alternate"):
        store.put_scope_chunk_map(dict(scope_snapshot_id=identifier, retrieval_version_id="rv", chunk_id="chunk"))
    initial = manager.start("rv", "first", scope, expected(), {})
    manager.validate("rv", "first", receipt, attempt=initial["attempt"])
    manager.publish("rv", "first", 0)
    with pytest.raises(RagIntegrityError, match="exactly"):
        manager.pin(scope)
    store.put_retrieval_version(dict(retrieval_version_id="rv2", material_version_id="mv2", profile={}, profile_hash="c" * 64))
    store.put_chunk(dict(retrieval_version_id="rv2", material_version_id="mv2", chunk_id="chunk", source_text="0123456789"),
                    [dict(scope["allowed_spans"][0], material_version_id="mv2")])
    second = manager.start("rv2", "second", second_scope, expected(), {})
    manager.validate("rv2", "second", receipt, attempt=second["attempt"])
    manager.publish("rv2", "second", 0)
    with pytest.raises(RagIntegrityError, match="mixed"):
        manager.pin(scope)
    third = manager.start("rv2", "third", scope, expected(), {})
    manager.validate("rv2", "third", receipt, attempt=third["attempt"])
    manager.publish("rv2", "third", 1)
    assert {m["manifest_id"] for m in manager.pin(scope)} == {"first", "third"}


@pytest.mark.parametrize("status", ["draft", "needs_replan"])
def test_available_space_states_can_build_and_pin(lifecycle, status):
    manager, _, engine, scope = lifecycle
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["learning_spaces"].update().values(status=status))
    ready(lifecycle)
    manager.publish("rv", "manifest", 0)
    assert manager.pin(scope)[0]["status"] == "ready"


def test_start_requires_explicit_retry_for_building_and_failed(lifecycle):
    manager, _, _, _ = lifecycle
    first = start(lifecycle)
    with pytest.raises(RagConflict):
        start(lifecycle)
    manager.mark_failed("rv", "manifest", attempt=first["attempt"], reason="interrupted")
    with pytest.raises(RagConflict):
        start(lifecycle)
    second = start(lifecycle, retry=True)
    assert second["attempt"] != first["attempt"]
    assert second["status"] == "building"


def test_external_adapter_conflict_fails_owner_without_persisting_secrets(lifecycle):
    manager, store, _, _ = lifecycle
    manifest = start(lifecycle)
    def validator(m, chunks):
        raise RagConflict("provider failed with secret=credential")
    with pytest.raises(RagConflict):
        manager.validate("rv", "manifest", validator, attempt=manifest["attempt"])
    result = store.get_manifest("rv", "manifest")
    assert result["status"] == "failed"
    assert result["failure"] == "VALIDATION_FAILED"


def test_ready_validation_rechecks_sql_before_and_after_external_readback(lifecycle):
    manager, store, engine, _ = lifecycle
    ready(lifecycle)
    assert manager.validate_ready("rv", "manifest", receipt)["dense"]["verified"]
    def changed(manifest, chunks):
        result = receipt(manifest, chunks)
        with engine.begin() as connection:
            connection.execute(Base.metadata.tables["rag_scope_chunk_map"].delete())
        return result
    with pytest.raises(RagIntegrityError):
        manager.validate_ready("rv", "manifest", changed)
    assert store.get_manifest("rv", "manifest")["status"] == "ready"


def test_ready_validation_rejects_missing_chunks_before_external_call(lifecycle):
    manager, _, engine, _ = lifecycle
    ready(lifecycle)
    with engine.begin() as connection:
        for name in ("rag_scope_chunk_map", "rag_chunk_spans", "rag_chunks"):
            connection.execute(Base.metadata.tables[name].delete())
    def external(*args):
        pytest.fail("missing SQL chunks must fail before external verification")
    with pytest.raises(RagIntegrityError):
        manager.validate_ready("rv", "manifest", external)


def test_ready_validation_rejects_missing_b1_tree(lifecycle):
    manager, store, engine, _ = lifecycle
    store.put_node(dict(retrieval_version_id="rv", node_id="root", kind="root"))
    store.put_node(dict(retrieval_version_id="rv", node_id="leaf", parent_node_id="root", chunk_id="chunk", kind="leaf"))
    manifest = start(lifecycle, tree_version_id="tree")
    manager.validate("rv", "manifest", lambda m, c: dict(receipt(m, c),
        tree={"verified": True, "tree_version_id": "tree"}), attempt=manifest["attempt"])
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["rag_nodes"].delete().where(Base.metadata.tables["rag_nodes"].c.node_id == "leaf"))
    with pytest.raises(RagIntegrityError, match="tree"):
        manager.validate_ready("rv", "manifest", receipt)
