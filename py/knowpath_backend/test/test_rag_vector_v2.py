"""New content identities tested against the real in-memory Qdrant engine."""
import importlib
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models


def api():
    return importlib.import_module("knowpath_backend.learning.rag.vector")


def chunk(identifier="a", version="r1", text="Title\nbody"):
    return {"chunk_id": identifier, "retrieval_version_id": version, "material_version_id": "m1",
            "source_text": "body", "retrieval_text": text, "source_spans": []}


@pytest.fixture
def index():
    client = QdrantClient(":memory:")
    try:
        yield api().QdrantContentIndex(client, "rag_test", 3, "fake-model-v1")
    finally:
        client.close()


def test_readback_receipt_includes_minimal_provenance_and_no_source_body(index):
    row = chunk()
    index.upsert([row], [[1., 0., 0.]])
    receipt = index.verify([row])
    assert receipt["verified"] is True
    assert receipt["chunks"][0]["chunk_id"] == "a"
    points, _ = index.client.scroll(index.collection, with_vectors=True)
    assert points[0].payload["content_hash"] == receipt["chunks"][0]["content_hash"]
    assert not {"source_text", "retrieval_text", "source_spans", "graph_version"} & points[0].payload.keys()
    assert index.search([1., 0., 0.], [row], 10) == [("a", pytest.approx(1.))]


def test_search_exact_allowed_ids_and_retrieval_version(index):
    allowed, hidden, old = chunk(), chunk("hidden"), chunk(version="old")
    index.upsert([allowed, hidden, old], [[0., 1., 0.], [1., 0., 0.], [1., 0., 0.]])
    assert [i for i, _ in index.search([1., 0., 0.], [allowed], 10)] == ["a"]
    assert index.search([1., 0., 0.], [], 10) == []


def test_unconfirmed_mutation_is_not_treated_as_complete(index, monkeypatch):
    monkeypatch.setattr(index.client, 'upsert', lambda *a, **kw:
        models.UpdateResult(operation_id=1, status=models.UpdateStatus.ACKNOWLEDGED))
    with pytest.raises(Exception, match='VECTOR_WRITE_UNCONFIRMED'):
        index.upsert([chunk()], [[1., 0., 0.]])


def test_verification_reads_actual_payload_and_detects_title_change(index):
    row = chunk()
    index.upsert([row], [[1., 0., 0.]])
    with pytest.raises(Exception, match="VECTOR_INDEX_NOT_READY"):
        index.verify([chunk(text="Other title\nbody")])
    point = index.client.scroll(index.collection)[0][0]
    index.client.set_payload(index.collection, {"embedding_profile": "wrong"}, [point.id])
    with pytest.raises(Exception, match="VECTOR_INDEX_NOT_READY"):
        index.verify([row])


def test_verification_rejects_zero_vector_even_when_count_and_payload_match(index):
    row = chunk()
    index.upsert([row], [[1., 0., 0.]])
    point = index.client.scroll(index.collection)[0][0]
    index.client.upsert(index.collection, [models.PointStruct(id=point.id, vector=[0., 0., 0.], payload=point.payload)])
    with pytest.raises(Exception, match="VECTOR_INDEX_NOT_READY"):
        index.verify([row])


def test_profiles_use_different_collections_and_delete_only_requested_version(index):
    newer = api().QdrantContentIndex(index.client, "rag_test", 3, "fake-model-v2")
    assert newer.collection != index.collection
    index.upsert([chunk(), chunk("b", "r2")], [[1., 0., 0.], [0., 1., 0.]])
    index.delete_retrieval_version("r1")
    assert index.verify([chunk("b", "r2")])["chunks"][0]["chunk_id"] == "b"
    with pytest.raises(Exception, match="VECTOR_INDEX_NOT_READY"):
        index.verify([chunk()])


@pytest.mark.parametrize("vector", [[0., 0., 0.], [1., 0.], [float("nan"), 1., 0.], [True, 0., 0.]])
def test_rejects_invalid_vectors(index, vector):
    with pytest.raises(Exception, match="EMBEDDING_INVALID_RESPONSE"):
        index.upsert([chunk()], [vector])
    with pytest.raises(Exception, match="EMBEDDING_INVALID_RESPONSE"):
        index.search(vector, [chunk()], 10)


def test_search_rejects_unexpected_ids_and_accepts_partial_topk(index, monkeypatch):
    index.upsert([chunk()], [[1., 0., 0.]])
    monkeypatch.setattr(index.client, "query_points", lambda *a, **k: SimpleNamespace(points=[]))
    assert index.search([1., 0., 0.], [chunk()], 10) == []
    monkeypatch.setattr(index.client, "query_points", lambda *a, **k: SimpleNamespace(points=[SimpleNamespace(id="unexpected", score=1.)]))
    with pytest.raises(Exception, match="VECTOR_INVALID_RESPONSE"):
        index.search([1., 0., 0.], [chunk()], 10)


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5])
def test_search_validates_limit_even_for_empty_scope(index, limit):
    with pytest.raises(ValueError):
        index.search([1., 0., 0.], [], limit)


@pytest.mark.parametrize("profile", [" ", {"dimension": 4, "model": "fake"}, {"dimension": True}, {"model": float("nan")}])
def test_profile_rejects_inconsistent_or_nonfinite_provenance(index, profile):
    with pytest.raises(ValueError):
        api().QdrantContentIndex(index.client, "rag_test", 3, profile)


def test_profile_identity_is_canonical_and_independent_of_graph(index):
    a = api().QdrantContentIndex(index.client, "rag_test", 3, {"model": "fake", "dimension": 3})
    b = api().QdrantContentIndex(index.client, "rag_test", 3, {"dimension": 3, "model": "fake"})
    assert a.collection == b.collection
    assert a.collection_prefix == "rag_test"
    assert api().point_id(chunk() | {"graph_version": 1}) == api().point_id(chunk() | {"graph_version": 2})
    assert api().point_id(chunk()) != api().point_id(chunk(text="new title\nbody"))


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True])
def test_search_rejects_invalid_scores(index, monkeypatch, score):
    index.upsert([chunk()], [[1., 0., 0.]])
    point = index.client.scroll(index.collection)[0][0]
    monkeypatch.setattr(index.client, "query_points", lambda *a, **k: SimpleNamespace(points=[
        SimpleNamespace(id=point.id, payload=point.payload, score=score)]))
    with pytest.raises(Exception, match="VECTOR_INVALID_RESPONSE"):
        index.search([1., 0., 0.], [chunk()], 10)


def test_profile_collection_with_wrong_shape_is_rejected_without_deletion(index):
    index.client.create_collection(index.collection, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    with pytest.raises(Exception, match="VECTOR_PROFILE_MISMATCH"):
        index.upsert([chunk()], [[1., 0., 0.]])
    assert index.client.get_collection(index.collection).config.params.vectors.size == 2
