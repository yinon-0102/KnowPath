"""Evidence preparation uses the production scope path without drafting answers."""
import copy

import pytest

from knowpath_backend.test.test_rag_building import build_workspace
from knowpath_backend.test.test_rag_pipeline import pipeline
from knowpath_backend.learning.rag.verification import VerificationError


def prepare(service, space_id):
    method = getattr(service, 'prepare_evidence', None)
    assert callable(method), 'pipeline needs generation-free evidence preparation'
    return method('学校应该告知谁？', space_id=space_id)


def test_prepare_evidence_never_generates_and_preserves_authoritative_trace(build_workspace, monkeypatch):
    service, reranker = pipeline(build_workspace)
    def forbidden(*args, **kwargs):
        pytest.fail('evidence preparation must never generate an answer')
    monkeypatch.setattr(service.verifier, 'answer', forbidden)
    result = prepare(service, build_workspace[2]['id'])
    assert result.status == 'ready'
    assert result.sources and reranker.calls
    assert result.trace['context_ids'] == [row['chunk_id'] for row in result.sources]
    assert result.trace['manifest_ids']
    assert result.trace['request_scope']['space_id'] == build_workspace[2]['id']
    assert result.trace['generation_calls'] == result.trace['verification_calls'] == 0


def test_prepare_checks_expected_scope_before_retrieval(build_workspace):
    service, reranker = pipeline(build_workspace)
    assert callable(getattr(service, 'prepare_evidence', None))
    with pytest.raises(VerificationError, match='STALE_LEARNING_CONTEXT'):
        service.prepare_evidence('学校？', space_id=build_workspace[2]['id'], expected_scope_version=999)
    assert reranker.calls == []


def test_prepare_clarification_has_no_prose_and_answer_keeps_legacy_response(build_workspace, monkeypatch):
    service, reranker = pipeline(build_workspace)
    def forbidden(*args, **kwargs):
        pytest.fail('clarification must not generate')
    monkeypatch.setattr(service.verifier, 'answer', forbidden)
    prepared = service.prepare_evidence('那么它呢？', space_id=build_workspace[2]['id'])
    assert prepared.status == 'clarify'
    assert prepared.sources == []
    assert not hasattr(prepared, 'text')
    result = service.answer('那么它呢？', space_id=build_workspace[2]['id'])
    assert result['status'] == 'clarify'
    assert result['text'] == '请明确本轮问题指向的对象或资料范围。'
    assert result['trace']['generation_calls'] == 0
    assert reranker.calls == []


def test_prepare_cancelled_before_retrieval(build_workspace):
    service, reranker = pipeline(build_workspace)
    with pytest.raises(VerificationError, match='RAG_CANCELLED'):
        service.prepare_evidence('学校通知谁？', space_id=build_workspace[2]['id'], cancelled=lambda: True)
    assert reranker.calls == []


def test_registry_ids_unique_across_retrievals_and_copies_cannot_grant_sources(build_workspace):
    from knowpath_backend.learning.rag import pipeline as pipeline_module
    registry_type = getattr(pipeline_module, 'EvidenceRegistry', None)
    assert registry_type is not None, 'request-local authoritative evidence registry missing'
    service, _ = pipeline(build_workspace)
    prepared = prepare(service, build_workspace[2]['id'])
    original = copy.deepcopy(prepared.sources)
    registry = registry_type()
    first = registry.register(prepared)
    second = registry.register(prepare(service, build_workspace[2]['id']))
    ids = [row['evidence_id'] for row in first + second]
    assert len(ids) == len(set(ids))
    first[0]['source_text'] = 'forged'
    prepared.sources[0]['source_text'] = 'mutated'
    resolved = registry.resolve([ids[0]])
    assert resolved == original[:1]
    resolved[0]['source_spans'].clear()
    assert registry.resolve([ids[0]]) == original[:1]
    with pytest.raises(VerificationError, match='RAG_SOURCE_INVALID'):
        registry.resolve([ids[0], 'E999999'])
