"""Build real lexical/local-vector indexes from immutable learning sources."""
import hashlib

import pytest
import sqlalchemy as sa
from qdrant_client import QdrantClient

from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.persistence.rag_repository import SqlRagRepository
from knowpath_backend.learning.rag.building import RagBuilder
from knowpath_backend.learning.rag.vector import QdrantContentIndex


class Embeddings:
    dimension = 4
    model_version = "deterministic-test-v1"
    def __init__(self):
        self.calls = []
        self.fail = False
    def embed(self, texts, *, query=False):
        self.calls.extend(texts)
        if self.fail:
            raise RuntimeError("injected embedding failure")
        return [[float(byte + 1) for byte in hashlib.sha256(t.encode()).digest()[:4]] for t in texts]


@pytest.fixture
def build_workspace(tmp_path):
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'build.db').as_posix()}")
    sa.event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    init_db(engine)
    state = LearningState(SqlAlchemyMaterialRepository(engine))
    uploaded = state.material_service.create(filename="rules.md",
        content="# 范围\n\n学校应当告知监护人。\n\n# 排除\n\n未授权内容不得进入回答。".encode(), idempotency_key="build")
    space = state.create_space(dict(name="Build", material_ids=[uploaded.material.id]))
    repo = SqlRagRepository(engine)
    embedder = Embeddings()
    client = QdrantClient(":memory:")
    dense = QdrantContentIndex(client, "rag_build_test", 4, embedder.model_version)
    builder = RagBuilder(repo, state.material_service.repository, state.space_service, embedder, dense)
    yield state, uploaded, space, repo, embedder, dense, builder
    client.close()
    engine.dispose()


def test_build_validates_then_explicitly_publishes_without_rewriting_old_sources(build_workspace):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    result = builder.build(space["id"], uploaded.version.id)
    assert result["status"] == "ready" and result["a_ready"]
    assert not result["b1_ready"]
    original = state.material_service.repository.get_version(uploaded.version.id)
    assert original.chunks == uploaded.version.chunks
    assert embedder.calls and all("学校" in t or "未授权" in t for t in embedder.calls)
    generation = builder.publish(result, expected_generation=0)
    assert generation == 1
    pinned = builder.lifecycle.pin(state.space_service.rag_scope_snapshot(space["id"]))
    assert pinned[0]["manifest_id"] == result["manifest_id"]


def test_tree_build_reuses_identical_leaves_and_embeddings(build_workspace):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    ordinary = builder.build(space['id'], uploaded.version.id)
    before = list(embedder.calls)
    tree = builder.build_tree(ordinary)
    assert tree['a_ready'] and tree['b1_ready']
    assert tree['retrieval_version_id'] == ordinary['retrieval_version_id']
    assert tree['manifest_id'] != ordinary['manifest_id']
    assert embedder.calls == before
    assert builder.build_tree(ordinary)['manifest_id'] == tree['manifest_id']
    builder.publish(tree, expected_generation=0)
    assert builder.lifecycle.pin(state.space_service.rag_scope_snapshot(space['id']), require_b1=True)
    nodes = repo.list_nodes(tree['retrieval_version_id'])
    assert {n['chunk_id'] for n in nodes if n.get('chunk_id')} == {
        c['chunk_id'] for c in repo.list_chunks(tree['retrieval_version_id'])}


def test_build_reuses_ready_version_and_scope_mapping_excludes_unselected_chunks(build_workspace):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    topics = state.space_service.bound_topics(space)
    allowed = next(t for t in topics if t["name"] == "范围")
    denied = next(t for t in topics if t["name"] == "排除")
    state.space_service.set_scope(space["id"], dict(topic_ids=[allowed["id"]],
        excluded_topic_ids=[denied["id"]], expected_version=1))
    result = builder.build(space["id"], uploaded.version.id)
    before = len(embedder.calls)
    replay = builder.build(space["id"], uploaded.version.id)
    assert replay["manifest_id"] == result["manifest_id"] and len(embedder.calls) == before
    scope = state.space_service.rag_scope_snapshot(space["id"])
    candidates = repo.list_scope_chunks(scope.scope_snapshot_id, result["retrieval_version_id"])
    assert candidates and all("未授权" not in c["source_text"] for c in candidates)


def test_embedding_failure_leaves_previous_publication_and_can_resume(build_workspace):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    old = builder.build(space["id"], uploaded.version.id)
    builder.publish(old, expected_generation=0)
    embedder.fail = True
    with pytest.raises(Exception):
        builder.build(space["id"], uploaded.version.id, max_tokens=1024)
    assert builder.lifecycle.pin(state.space_service.rag_scope_snapshot(space["id"]))[0]["manifest_id"] == old["manifest_id"]
    embedder.fail = False
    restored = builder.build(space["id"], uploaded.version.id, max_tokens=1024, retry=True)
    assert restored["status"] == "ready"


def test_build_refuses_unbound_original_before_external_call(build_workspace):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    other = state.material_service.create(filename="other.txt", content=b"Outside scope", idempotency_key="outside")
    with pytest.raises(ValueError, match="bound"):
        builder.build(space["id"], other.version.id)
    assert embedder.calls == []


def _write_audits(repository):
    from knowpath_backend.learning.persistence.db import Base
    table = Base.metadata.tables["outbox_events"]
    # Independent connection proves call reservations committed before the
    # simulated provider is entered, rather than existing only in local memory.
    with repository.engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table).where(
            table.c.event_type == "rag.index.write").order_by(table.c.created_at, table.c.id)).mappings()]


def _audit_document(state):
    uploaded = state.material_service.create(filename="audit.md",
        content=("# Audit\n\n" + "\n\n".join(f"Original paragraph {i}." for i in range(12))).encode(),
        idempotency_key="embedding-audit")
    space = state.create_space(dict(name="Audit", material_ids=[uploaded.material.id]))
    return uploaded, space


def test_later_embedding_failure_preserves_success_and_unknown_billing_across_retry(build_workspace, monkeypatch):
    import copy
    import json
    from knowpath_backend.learning.rag.retrieval import RetrievalError
    state, _, _, repo, embedder, _, builder = build_workspace
    uploaded, space = _audit_document(state)
    embed = embedder.embed
    calls, reservations = [], []
    fail = [True]

    def observed_embed(texts, *, query=False):
        calls.append(len(texts))
        records = _write_audits(repo)
        assert records
        reservation = records[-1]["payload"].get("embedding_calls", [])
        assert reservation, "embedding call must be durably reserved before the provider"
        reservations.append(copy.deepcopy(reservation[-1]))
        assert reservation[-1]["status"] == "started" and reservation[-1]["billing_unknown"] is True
        assert reservation[-1]["input_count"] == len(texts)
        if len(calls) == 2 and fail[0]:
            # last_usage intentionally still contains the first success.
            raise RetrievalError("provider-secret-exception")
        values = embed(texts, query=query)
        embedder.last_usage = {"model": embedder.model_version, "calls": 1,
            "input_tokens": len(texts) * 7, "complete": True,
            "request_text": "provider-secret-body", "headers": {"key": "provider-secret-key"}}
        return values

    monkeypatch.setattr(embedder, "embed", observed_embed)
    with pytest.raises(RetrievalError, match="INDEX_BUILD_FAILED"):
        builder.build(space["id"], uploaded.version.id)
    old = _write_audits(repo)[0]
    first, failed = old["payload"]["embedding_calls"]
    assert calls == [10, 2]
    assert first["status"] == "succeeded" and first["billing_unknown"] is False
    assert first["usage"]["input_tokens"] == 70
    assert first["elapsed_seconds"] >= 0 and first["finished_at"]
    assert failed["status"] == "failed" and failed["billing_unknown"] is True
    assert failed["usage"]["input_tokens"] is None
    assert failed["finished_at"] and failed["elapsed_seconds"] >= 0
    assert "provider-secret" not in json.dumps(old["payload"])
    from knowpath_backend.learning.persistence.rag_models import TABLES
    with repo.engine.connect() as connection:
        manifests = list(connection.execute(sa.select(TABLES["rag_manifests"].c.payload).where(
            TABLES["rag_manifests"].c.retrieval_version_id == old["payload"]["retrieval_version_id"])).scalars())
    assert "provider-secret" not in json.dumps(manifests)

    fail[0] = False
    ready = builder.build(space["id"], uploaded.version.id, retry=True)
    history = _write_audits(repo)
    assert ready["status"] == "ready"
    assert len(history) == 2 and history[0]["id"] != history[1]["id"]
    assert history[0]["payload"] == old["payload"]
    assert calls == [10, 2, 2]  # Retry reuses the verified first vector batch.
    retried = history[1]["payload"]["embedding_calls"]
    assert len(retried) == 1 and retried[0]["input_count"] == 2
    assert retried[0]["status"] == "succeeded" and retried[0]["usage"]["input_tokens"] == 14
    assert "provider-secret" not in json.dumps(ready)


def test_process_crash_keeps_embedding_reservation_and_billing_unknown(build_workspace, monkeypatch):
    state, uploaded, space, repo, embedder, _, builder = build_workspace
    class ProcessCrash(BaseException):
        pass
    def crash(*args, **kwargs):
        raise ProcessCrash()
    monkeypatch.setattr(embedder, "embed", crash)
    with pytest.raises(ProcessCrash):
        builder.build(space["id"], uploaded.version.id)
    intent = _write_audits(repo)[0]
    assert intent["status"] == "processing"
    records = intent["payload"].get("embedding_calls", [])
    assert len(records) == 1
    assert records[0]["status"] == "started" and records[0]["billing_unknown"] is True
    assert records[0]["started_at"] and records[0]["finished_at"] is None
    assert records[0]["model"] == embedder.model_version


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 10, "complete": False},
    {"input_tokens": True, "complete": True}, {"input_tokens": float("nan"), "complete": True}])
def test_successful_embedding_without_valid_usage_does_not_claim_known_billing(build_workspace, monkeypatch, usage):
    _, uploaded, space, repo, embedder, _, builder = build_workspace
    embed = embedder.embed
    def incomplete_usage(*args, **kwargs):
        vectors = embed(*args, **kwargs)
        embedder.last_usage = usage
        return vectors
    monkeypatch.setattr(embedder, "embed", incomplete_usage)
    ready = builder.build(space["id"], uploaded.version.id)
    assert ready["status"] == "ready"
    record = _write_audits(repo)[0]["payload"]["embedding_calls"][0]
    assert record["status"] == "succeeded" and record["billing_unknown"] is True


def test_embedding_audit_reservation_failure_prevents_provider_call(build_workspace, monkeypatch):
    from knowpath_backend.learning.rag import building
    _, uploaded, space, _, embedder, _, builder = build_workspace
    def unavailable(*args, **kwargs):
        raise RuntimeError("private audit storage failure")
    monkeypatch.setattr(building, "reserve_rag_embedding", unavailable)
    with pytest.raises(Exception, match="INDEX_BUILD_FAILED"):
        builder.build(space["id"], uploaded.version.id)
    assert embedder.calls == []


def test_lost_success_acknowledgement_keeps_reservation_unknown(build_workspace, monkeypatch):
    from knowpath_backend.learning.rag import building
    _, uploaded, space, repo, embedder, _, builder = build_workspace
    def unavailable(*args, **kwargs):
        raise RuntimeError("private audit acknowledgement failure")
    monkeypatch.setattr(building, "finish_rag_embedding", unavailable)
    with pytest.raises(Exception, match="INDEX_BUILD_FAILED"):
        builder.build(space["id"], uploaded.version.id)
    assert embedder.calls  # The provider ran, but the result was not durably acknowledged.
    record = _write_audits(repo)[0]["payload"]["embedding_calls"][0]
    assert record["status"] == "started" and record["billing_unknown"] is True
