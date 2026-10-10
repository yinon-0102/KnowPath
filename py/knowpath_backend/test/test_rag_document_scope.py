"""Document selection constrains real hybrid search, reranking and model evidence."""
import copy
import json

import pytest

from knowpath_backend.test.test_rag_building import build_workspace
from knowpath_backend.test.test_rag_pipeline import Reranker, Generator, Checker
from knowpath_backend.learning.rag.pipeline import RagPipeline
from knowpath_backend.learning.rag.plugins import OrdinaryPlugin
from knowpath_backend.learning.rag.verification import AnswerVerifier, VerificationError


@pytest.fixture
def two_documents(build_workspace):
    state, a, _, repo, embedder, dense, builder = build_workspace
    b = state.material_service.create(filename='other.md', content='# Secret\n\nB_ONLY_SECRET unrelated document.'.encode(), idempotency_key='second')
    space = state.create_space({'name': 'Both', 'material_ids': [a.material.id, b.material.id]})
    for upload in (a, b):
        builder.publish(builder.build(space['id'], upload.version.id), expected_generation=0)
    reranker, prompts = Reranker(), []
    class RecordingGenerator(Generator):
        def generate_json(self, messages, **kwargs):
            prompts.append(copy.deepcopy(messages))
            return super().generate_json(messages, **kwargs)
    pipeline = RagPipeline(repo, state.material_service.repository, state.space_service,
        OrdinaryPlugin(embedder, dense), reranker, AnswerVerifier(RecordingGenerator(), Checker()))
    return state, space, a, b, pipeline, reranker, prompts, dense


def test_document_selection_restricts_vector_rerank_prompt_and_citations(two_documents, monkeypatch):
    state, space, a, b, pipeline, reranker, prompts, dense = two_documents
    candidates = []
    search = dense.search
    def record(vector, chunks, limit=30):
        candidates.extend(copy.deepcopy(chunks))
        return search(vector, chunks, limit=limit)
    monkeypatch.setattr(dense, 'search', record)
    result = pipeline.answer('学校的义务是什么？', space_id=space['id'], material_ids=[a.material.id])
    for rows in (candidates, reranker.calls[0], result['sources'], result['citations']):
        assert rows and {r['material_version_id'] for r in rows} == {a.version.id}
    assert 'B_ONLY_SECRET' not in json.dumps(prompts, ensure_ascii=False)
    assert result['trace']['request_scope']['material_ids'] == [a.material.id]
    assert result['trace']['scope_snapshot_id'] == state.space_service.rag_scope_snapshot(space['id']).scope_snapshot_id


def test_multiple_selected_documents_share_one_retrieval_pool(two_documents):
    _, space, a, b, pipeline, reranker, _, _ = two_documents
    pipeline.answer('学校义务', space_id=space['id'], material_ids=[a.material.id, b.material.id])
    assert {r['material_version_id'] for r in reranker.calls[0]} == {a.version.id, b.version.id}


def test_unbound_document_fails_before_embedding(two_documents):
    _, space, _, _, pipeline, reranker, prompts, _ = two_documents
    with pytest.raises(VerificationError) as error:
        pipeline.answer('q', space_id=space['id'], material_ids=['foreign'])
    assert error.value.code == 'RETRIEVAL_SCOPE_INVALID'
    assert not reranker.calls and not prompts


def test_message_passes_frozen_selection_to_rag(two_documents):
    state, space, a, _, pipeline, reranker, _, _ = two_documents
    state.message_service.rag_pipeline = pipeline
    job = state.send_message(space['id'], {'message': '学校义务', 'material_ids': [a.material.id]})
    assert state.get_run(job['run_id'])['status'] == 'succeeded'
    assert {r['material_version_id'] for r in reranker.calls[0]} == {a.version.id}
    response = next(e['data'] for e in state.events_for(job['run_id']) if e['event'] == 'message.completed')
    assert response['request_scope']['material_ids'] == [a.material.id]
