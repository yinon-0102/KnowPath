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


def test_message_passes_frozen_selection_to_rag(two_documents, monkeypatch):
    from knowpath_backend.core.model_port import SDKModelPort
    from knowpath_backend.learning.rag import runtime
    from knowpath_backend.test.web_agent_support import ScriptedChatClient, completion
    from knowpath_backend.test.test_web_agent_runtime import output, claim
    from knowpath_backend.test.test_rag_verification_v2 import verdict
    state, space, a, b, pipeline, reranker, _, dense = two_documents
    prepared = pipeline.prepare_evidence('学校义务', space_id=space['id'], material_ids=[a.material.id])
    source = prepared.sources[0]
    assert source['chunk_id'] not in {chunk.id for chunk in a.version.chunks}
    reranker.calls.clear()
    candidates = []
    search = dense.search
    def record(vector, chunks, limit=30):
        candidates.extend(copy.deepcopy(chunks))
        return search(vector, chunks, limit=limit)
    monkeypatch.setattr(dense, 'search', record)
    class OwnedDenseClient:
        def __init__(self, *args, **kwargs): pass
        def close(self): pass  # The fixture owns its local Qdrant store.
    monkeypatch.setattr(runtime, 'QdrantClient', OwnedDenseClient)
    monkeypatch.setattr(runtime, 'embedding_model', lambda *args, **kwargs: pipeline.plugin.embedder)
    monkeypatch.setattr(runtime, 'QdrantContentIndex', lambda *args, **kwargs: dense)
    monkeypatch.setattr(runtime, 'create_plugin', lambda *args, **kwargs: pipeline.plugin)
    monkeypatch.setattr(runtime, 'DashScopeReranker', lambda *args, **kwargs: reranker)
    configured = runtime.ConfiguredPipeline(state.material_service.repository, state.space_service,
        state.message_service.context_settings, 'a')
    state.message_service.rag_pipeline = configured
    state.message_service.use_agent_runtime = True
    client = ScriptedChatClient([
        completion(output(route='material', claims=[{**claim(), 'text': source['source_text']}])),
        completion(json.dumps(verdict())),
    ])
    state.message_service.agent_model_port = SDKModelPort(client)
    def forbidden(*args, **kwargs):
        pytest.fail('configured Web Agent invoked old final generation')
    monkeypatch.setattr(pipeline.verifier, 'answer', forbidden)
    monkeypatch.setattr(state.message_service, '_adaptive_answer', forbidden)
    monkeypatch.setattr(state.message_service, '_legacy_answer', forbidden)
    job = state.send_message(space['id'], {'message': '学校义务', 'material_ids': [a.material.id],
                                         'answer_mode': 'material'})
    assert state.get_run(job['run_id'])['status'] == 'succeeded', state.get_run(job['run_id'])
    assert {r['material_version_id'] for r in reranker.calls[0]} == {a.version.id}
    assert candidates and {r['material_version_id'] for r in candidates} == {a.version.id}
    assert 'B_ONLY_SECRET' not in json.dumps(client.requests, ensure_ascii=False)
    assert len(client.requests) == 2
    response = next(e['data'] for e in state.events_for(job['run_id']) if e['event'] == 'message.completed')
    assert response['request_scope']['material_ids'] == [a.material.id]
    assert response['citations'][0]['chunk_id'] == source['chunk_id']
    assert response['citations'][0]['retrieval_version_id'] == source['retrieval_version_id']
    message = next(row for row in state.message_service.repository.records('messages', space_id=space['id'])
                   if row['run_id'] == job['run_id'])
    assert state.message_service.repository.agent_memory_factory(space['id']).get_commit('turn:' + message['id'])
