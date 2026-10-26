"""Configured evidence operations keep request ownership without final generation."""
import time
from types import SimpleNamespace

import pytest

from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.runtime import ConfiguredPipeline
from knowpath_backend.learning.rag.verification import Draft, VerificationError
from knowpath_backend.test.test_rag_building import build_workspace
from knowpath_backend.test.test_rag_pipeline import pipeline, Checker


def configured(fixture):
    state = fixture[0]
    return ConfiguredPipeline(state.material_repository, state.space_service, LearningSettings(), 'a')


def test_preparation_dispatches_without_legacy_answer_and_returns_detached_trace(build_workspace, monkeypatch):
    instance = configured(build_workspace)
    service, _ = pipeline(build_workspace)
    assert callable(getattr(instance, 'prepare_evidence', None))
    def execute(question, *, operation='answer', **kwargs):
        assert operation == 'prepare'
        kwargs.pop('deadline'); kwargs.pop('journal'); kwargs.pop('spending')
        return service.prepare_evidence(question, **kwargs)
    monkeypatch.setattr(instance, '_answer', execute)
    result = instance.prepare_evidence('学校应该告知谁？', space_id=build_workspace[2]['id'])
    assert result.sources
    assert result.trace['call_journal']['physical_calls'] == 0


def test_check_only_uses_current_authoritative_sources_without_retrieval_or_generator(build_workspace, monkeypatch):
    from knowpath_backend.learning.rag import runtime
    service, _ = pipeline(build_workspace)
    prepared = service.prepare_evidence('学校应该告知谁？', space_id=build_workspace[2]['id'])
    source = next(s for s in prepared.sources if '学校' in s['source_text'])
    candidate = Draft.model_validate(dict(status='answered', claims=[dict(claim_id='one',
        text='学校应当告知监护人。', kind='fact', citation_ids=[source['chunk_id']], depends_on=[])], missing_points=[]))
    instance = configured(build_workspace)
    assert callable(getattr(instance, 'verify_candidate', None))
    def forbidden(*args, **kwargs):
        pytest.fail('check-only operation initialized retrieval or generator')
    monkeypatch.setattr(runtime, 'embedding_model', forbidden)
    monkeypatch.setattr(runtime, 'QdrantClient', forbidden)
    monkeypatch.setattr(runtime, 'BudgetedJsonModel', lambda *args, **kwargs: Checker() if kwargs['stage'] == 'verification' else forbidden())
    result = instance.verify_candidate('学校应该告知谁？', prepared.sources, candidate,
        space_id=build_workspace[2]['id'], deadline=time.monotonic()+10,
        expected_scope_version=prepared.scope.scope_version)
    assert result['status'] == 'answered'
    assert result['trace']['generation_calls'] == 0
    forged = [{**s, 'source_text':'forged'} for s in prepared.sources]
    with pytest.raises(VerificationError, match='RAG_SOURCE_INVALID'):
        instance.verify_candidate('问题', forged, candidate, space_id=build_workspace[2]['id'], deadline=time.monotonic()+10)


def test_shared_turn_budget_does_not_reset_deadline_journal_or_operation_limit(build_workspace, monkeypatch):
    instance = configured(build_workspace)
    assert callable(getattr(instance, 'new_turn_budget', None))
    budget = instance.new_turn_budget(deadline=time.monotonic()+10, max_operations=2)
    observed = []
    def execute(question, *, deadline, journal, spending, **kwargs):
        observed.append((deadline, journal, spending))
        return {'trace': {}}
    monkeypatch.setattr(instance, '_answer', execute)
    instance.answer('a', turn_budget=budget)
    instance.answer('b', turn_budget=budget)
    assert observed[0] == observed[1]
    assert budget.journal.snapshot()['status'] == 'running'
    with pytest.raises(VerificationError, match='RAG_STAGE_BUDGET_EXCEEDED'):
        instance.answer('c', turn_budget=budget)
    assert len(observed) == 2


@pytest.mark.parametrize('kind', ['cancelled', 'deadline'])
def test_configured_prepare_guards_before_provider_initialization(build_workspace, kind):
    instance = configured(build_workspace)
    assert callable(getattr(instance, 'prepare_evidence', None))
    kwargs = {'cancelled': lambda: True} if kind == 'cancelled' else {'deadline': time.monotonic()-1}
    with pytest.raises(VerificationError, match='RAG_CANCELLED' if kind == 'cancelled' else 'RAG_DEADLINE_EXCEEDED'):
        instance.prepare_evidence('问题', space_id=build_workspace[2]['id'], **kwargs)


def test_production_prepare_closes_clients_and_applies_source_allowlist_before_retrieval(build_workspace, monkeypatch):
    from knowpath_backend.learning.rag import runtime, capacity
    service, reranker = pipeline(build_workspace)
    originals = service._originals(service._scope(build_workspace[2]['id']),
        service.lifecycle.pin(service._scope(build_workspace[2]['id'])))
    allowed = next(row for row in originals if '学校' in row['source_text'])
    closed = []
    class DenseClient:
        def __init__(self, *args, **kwargs): pass
        def close(self): closed.append(True)
    monkeypatch.setattr(runtime, 'QdrantClient', DenseClient)
    monkeypatch.setattr(runtime, 'embedding_model', lambda *args, **kwargs: SimpleNamespace(model_version='offline', dimension=3))
    monkeypatch.setattr(runtime, 'QdrantContentIndex', lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, 'create_plugin', lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, 'BudgetedJsonModel', lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, 'DashScopeReranker', lambda *args, **kwargs: object())
    monkeypatch.setattr(capacity, 'AnswerCapacity', lambda *args, **kwargs: SimpleNamespace(admit_revision=None))
    monkeypatch.setattr(runtime, 'RagPipeline', lambda *args, **kwargs: service)
    monkeypatch.setattr(service.verifier, 'answer', lambda *args, **kwargs: pytest.fail('prepare generated final answer'))
    result = configured(build_workspace).prepare_evidence('学校应该告知谁？', space_id=build_workspace[2]['id'],
        allowed_sources=[allowed])
    assert closed == [True]
    assert result.sources
    assert {row['chunk_id'] for row in reranker.calls[0]} == {allowed['chunk_id']}
    assert result.trace['generation_calls'] == 0


@pytest.mark.parametrize('mutation', ['scope', 'allowlist'])
def test_configured_checker_enforces_source_allowlist_and_postcheck_scope(build_workspace, monkeypatch, mutation):
    from knowpath_backend.learning.rag import runtime
    service, _ = pipeline(build_workspace)
    prepared = service.prepare_evidence('学校应该告知谁？', space_id=build_workspace[2]['id'])
    source = next(s for s in prepared.sources if '学校' in s['source_text'])
    candidate = Draft.model_validate(dict(status='answered', claims=[dict(claim_id='one',
        text='学校应当告知监护人。', kind='fact', citation_ids=[source['chunk_id']], depends_on=[])], missing_points=[]))
    def hook():
        build_workspace[0].space_service.set_scope(build_workspace[2]['id'],
            {'topic_ids': ['topic_范围'], 'expected_version': 1})
    monkeypatch.setattr(runtime, 'BudgetedJsonModel', lambda *args, **kwargs: Checker(hook if mutation == 'scope' else None))
    with pytest.raises(VerificationError, match='STALE_LEARNING_CONTEXT' if mutation == 'scope' else 'RAG_SOURCE_INVALID'):
        configured(build_workspace).verify_candidate('学校应该告知谁？', prepared.sources, candidate,
            space_id=build_workspace[2]['id'], deadline=time.monotonic()+10,
            allowed_sources=[] if mutation == 'allowlist' else None)


def test_original_span_allowlist_authorizes_real_retrieval_ids_and_excludes_other_chunks(build_workspace, monkeypatch):
    from knowpath_backend.learning.rag import runtime, capacity
    service, reranker = pipeline(build_workspace)
    scope = service._scope(build_workspace[2]['id'])
    rows = service._originals(scope, service.lifecycle.pin(scope))
    allowed = next(row for row in rows if '学校' in row['source_text'])
    original_id = allowed['source_spans'][0]['block']
    assert original_id != allowed['chunk_id']
    allowlist = [{'material_id': allowed['material_id'], 'material_version_id': allowed['material_version_id'],
        'chunk_id': original_id, 'source_spans': allowed['source_spans']}]
    class DenseClient:
        def __init__(self, *args, **kwargs): pass
        def close(self): pass
    monkeypatch.setattr(runtime, 'QdrantClient', DenseClient)
    monkeypatch.setattr(runtime, 'embedding_model', lambda *args, **kwargs: SimpleNamespace(model_version='offline', dimension=3))
    monkeypatch.setattr(runtime, 'QdrantContentIndex', lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, 'create_plugin', lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, 'BudgetedJsonModel', lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, 'DashScopeReranker', lambda *args, **kwargs: object())
    monkeypatch.setattr(capacity, 'AnswerCapacity', lambda *args, **kwargs: SimpleNamespace(admit_revision=None))
    monkeypatch.setattr(runtime, 'RagPipeline', lambda *args, **kwargs: service)
    result = configured(build_workspace).prepare_evidence('学校应该告知谁？', space_id=scope.space_id,
        allowed_sources=allowlist)
    assert result.sources
    assert all(row['chunk_id'] == allowed['chunk_id'] for row in result.sources)
    assert {row['chunk_id'] for row in reranker.calls[0]} == {allowed['chunk_id']}
    assert all('未授权' not in row['source_text'] for row in result.sources)


def test_retrieval_allowlist_cannot_exchange_id_for_same_original_spans(build_workspace):
    service, _ = pipeline(build_workspace)
    prepared = service.prepare_evidence('学校应该告知谁？', space_id=build_workspace[2]['id'])
    row = prepared.sources[0]
    with pytest.raises(VerificationError, match='RAG_SOURCE_INVALID'):
        ConfiguredPipeline._check_allowed([row], [{**row, 'chunk_id': 'forged-retrieval-id'}])
