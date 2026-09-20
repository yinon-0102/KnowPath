"""Ordinary RAG joins only pinned, scoped, verified original evidence."""
import copy

import pytest

from knowpath_backend.test.test_rag_building import build_workspace
from knowpath_backend.learning.rag.pipeline import RagPipeline
from knowpath_backend.learning.rag.plugins import OrdinaryPlugin
from knowpath_backend.learning.rag.verification import AnswerVerifier, VerificationError


class Reranker:
    def __init__(self): self.calls = []
    def rerank(self, question, chunks, *, deadline):
        self.calls.append(copy.deepcopy(chunks))
        return chunks


class Generator:
    def generate_json(self, messages, *, deadline):
        import json
        data = json.loads(messages[-1]["content"])
        evidence = next(s for s in data["evidence"] if "学校" in s["source_text"])
        return dict(status="answered", claims=[dict(claim_id="one", text="学校应当告知监护人。", kind="fact",
            citation_ids=[evidence["chunk_id"]], depends_on=[])], missing_points=[])


class Checker:
    def __init__(self, hook=None): self.hook = hook
    def generate_json(self, messages, *, deadline):
        import json
        draft = json.loads(messages[-1]["content"])["draft"]
        if self.hook: self.hook()
        return dict(checks=[dict(claim_id=c["claim_id"], status="supported", citation_ids=c["citation_ids"],
            reason="direct text") for c in draft["claims"]], missing_points=[], complete=True)


def pipeline(fixture, checker=None):
    state, uploaded, space, repo, embedder, dense, builder = fixture
    manifest = builder.build(space["id"], uploaded.version.id)
    builder.publish(manifest, expected_generation=0)
    reranker = Reranker()
    instance = RagPipeline(repo, state.material_service.repository, state.space_service,
        OrdinaryPlugin(embedder, dense), reranker, AnswerVerifier(Generator(), checker or Checker()))
    return instance, reranker


def test_pipeline_returns_v2_citations_only_from_current_scope(build_workspace):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    topics = state.space_service.bound_topics(space)
    selected = next(t for t in topics if t["name"] == "范围")
    excluded = next(t for t in topics if t["name"] == "排除")
    state.space_service.set_scope(space["id"], dict(topic_ids=[selected["id"]],
        excluded_topic_ids=[excluded["id"]], expected_version=1))
    service, reranker = pipeline(build_workspace)
    result = service.answer("学校应该告知谁？", space_id=space["id"])
    assert result["status"] == "answered"
    assert result["citations"][0]["citation_schema_version"] == 2
    assert result["citations"][0]["material_version_id"] == uploaded.version.id
    assert all("未授权" not in c["source_text"] for c in reranker.calls[0])
    assert result["trace"]["manifest_ids"] and result["sources"]


def test_scope_change_after_model_call_blocks_publication(build_workspace):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    def change():
        state.space_service.set_scope(space["id"], dict(topic_ids=["topic_范围"], expected_version=1))
    service, _ = pipeline(build_workspace, Checker(change))
    with pytest.raises(VerificationError) as error:
        service.answer("学校应该告知谁？", space_id=space["id"])
    assert error.value.code == "STALE_LEARNING_CONTEXT"


def test_expected_scope_is_checked_before_external_calls(build_workspace):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    service, reranker = pipeline(build_workspace)
    with pytest.raises(VerificationError):
        service.answer("学校？", space_id=space["id"], expected_scope_version=999)
    assert reranker.calls == []


def test_trace_preserves_actual_embedding_usage_and_candidate_source_spans(build_workspace):
    service, _ = pipeline(build_workspace)
    service.plugin.embedder.last_usage = {'model': 'test', 'input_tokens': 12, 'complete': True}
    result = service.answer('学校应该告知谁？', space_id=build_workspace[2]['id'])
    assert result['trace']['embedding_usage'] == service.plugin.embedder.last_usage
    for field in ('candidate_sources', 'reranked_sources'):
        assert result['trace'][field]
        assert all(set(row) == {'chunk_id', 'source_spans'} for row in result['trace'][field])


def test_missing_published_index_fails_instead_of_falling_back(build_workspace):
    state, uploaded, space, repo, embedder, dense, builder = build_workspace
    instance = RagPipeline(repo, state.material_service.repository, state.space_service,
        OrdinaryPlugin(embedder, dense), Reranker(), AnswerVerifier(Generator(), Checker()))
    with pytest.raises(VerificationError) as error:
        instance.answer("学校？", space_id=space["id"])
    assert error.value.code == "RAG_INDEX_NOT_READY"


def test_published_profile_mismatch_fails_before_query_embedding(build_workspace):
    service, _ = pipeline(build_workspace)
    before = len(service.plugin.embedder.calls)
    service.plugin.dense.collection = 'different_collection'
    with pytest.raises(VerificationError, match='VECTOR_PROFILE_MISMATCH'):
        service.answer('学校通知谁？', space_id=build_workspace[2]['id'])
    assert len(service.plugin.embedder.calls) == before


def test_missing_vectors_do_not_silently_use_only_bm25(build_workspace):
    service, _ = pipeline(build_workspace)
    scope = service.spaces.rag_scope_snapshot(build_workspace[2]['id'])
    manifest = service.lifecycle.pin(scope)[0]
    service.plugin.dense.delete_retrieval_version(manifest['retrieval_version_id'])
    with pytest.raises(Exception, match='VECTOR_INDEX_NOT_READY'):
        service.answer('学校通知谁？', space_id=scope.space_id)


def _document_pipeline(fixture, content, *, max_tokens=1800):
    """Use real SQL source mappings and local vectors for evidence regressions."""
    state, _, _, repo, embedder, dense, builder = fixture
    uploaded = state.material_service.create(filename="evidence.md", content=content.encode(),
        idempotency_key="pipeline-evidence")
    space = state.create_space(dict(name="Evidence", material_ids=[uploaded.material.id]))
    manifest = builder.build(space["id"], uploaded.version.id, max_tokens=max_tokens)
    builder.publish(manifest, expected_generation=0)

    class RecordingGenerator(Generator):
        def __init__(self):
            self.calls = []

        def generate_json(self, messages, *, deadline):
            self.calls.append(copy.deepcopy(messages))
            return super().generate_json(messages, deadline=deadline)

    generator, reranker = RecordingGenerator(), Reranker()
    service = RagPipeline(repo, state.material_service.repository, state.space_service,
        OrdinaryPlugin(embedder, dense), reranker, AnswerVerifier(generator, Checker()))
    return service, space, manifest, generator, reranker


@pytest.mark.parametrize("missing_stage", ["scope", "recall"])
def test_missing_continuation_member_prevents_incomplete_answer(build_workspace, monkeypatch, missing_stage):
    service, space, manifest, generator, reranker = _document_pipeline(build_workspace,
        "# 范围\n\n学校应当告知监护人。只有完成培训后才能申请。", max_tokens=40)
    repo = build_workspace[3]
    chunks = sorted(repo.list_chunks(manifest["retrieval_version_id"]), key=lambda row: row["ordinal"])
    assert len(chunks) >= 2
    first_id = chunks[0]["chunk_id"]
    assert all(row["continuation_of"] == first_id for row in chunks[1:])

    # A complete group really can answer; the negative case must fail because
    # evidence is missing, not because the fixture or its index is unusable.
    complete = service.answer("学校应该告知谁？", space_id=space["id"])
    assert complete["status"] == "answered"
    assert {row["chunk_id"] for row in complete["sources"]} == {row["chunk_id"] for row in chunks}
    calls_before = len(generator.calls)

    if missing_stage == "scope":
        list_scoped = repo.list_scope_chunks
        monkeypatch.setattr(repo, "list_scope_chunks", lambda *args, **kwargs: [
            row for row in list_scoped(*args, **kwargs) if row["chunk_id"] == first_id])
    else:
        retrieve = service.plugin.retrieve
        def first_only(*args, **kwargs):
            result = retrieve(*args, **kwargs)
            result["candidates"] = [row for row in result["candidates"] if row["chunk_id"] == first_id]
            return result
        monkeypatch.setattr(service.plugin, "retrieve", first_only)

    result = service.answer("学校应该告知谁？", space_id=space["id"])
    assert [row["chunk_id"] for row in reranker.calls[-1]] == [first_id]
    assert result["status"] == "insufficient"
    assert result["sources"] == result["citations"] == result["claims"] == []
    assert len(generator.calls) == calls_before
    assert result["trace"]["generation_calls"] == result["trace"]["verification_calls"] == 0


def test_damaged_source_is_omitted_before_retrieval_rerank_and_answer(build_workspace, monkeypatch):
    service, space, manifest, _, reranker = _document_pipeline(build_workspace,
        "# 范围\n\n学校应当告知监护人。\n\n学校�损坏正文不得作为依据。")
    chunks = build_workspace[3].list_chunks(manifest["retrieval_version_id"])
    damaged = {row["chunk_id"] for row in chunks if row["quality"]["content_status"] == "damaged"}
    assert damaged
    admitted = []
    retrieve = service.plugin.retrieve
    def record_scope(request, rows):
        admitted.extend(row["chunk_id"] for row in rows)
        return retrieve(request, rows)
    monkeypatch.setattr(service.plugin, "retrieve", record_scope)
    result = service.answer("学校应该告知谁？", space_id=space["id"])
    assert result["status"] == "answered"
    assert set(admitted) == {row["chunk_id"] for row in chunks} - damaged
    assert not damaged.intersection(row["chunk_id"] for row in reranker.calls[-1])
    assert not damaged.intersection(row["chunk_id"] for row in result["sources"])
    assert not damaged.intersection(row["chunk_id"] for row in result["citations"])
