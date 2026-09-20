"""Opt-in real services: unique collection and isolated SQL fixture only."""
import os
import json
import time
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from knowpath_backend.test.test_rag_building import build_workspace
from knowpath_backend.learning.rag.building import RagBuilder
from knowpath_backend.learning.rag.vector import QdrantContentIndex
from knowpath_backend.learning.rag.pipeline import RagPipeline
from knowpath_backend.learning.rag.plugins import OrdinaryPlugin
from knowpath_backend.learning.rag.tree import TreePlugin
from knowpath_backend.learning.rag.verification import AnswerVerifier
from knowpath_backend.learning.rag.model_services import BudgetedJsonModel
from knowpath_backend.learning.rag.reranking import DashScopeReranker


def test_real_qdrant_build_publish_tree_and_scope(build_workspace):
    url = os.getenv('RAG_TEST_QDRANT_URL')
    if not url:
        pytest.skip('requires dedicated opt-in Qdrant URL')
    state, uploaded, space, repo, embedder, _, _ = build_workspace
    client = QdrantClient(url=url, timeout=15, check_compatibility=False)
    dense = QdrantContentIndex(client, 'knowpath_rag_test_' + uuid4().hex, 4, embedder.model_version)
    try:
        builder = RagBuilder(repo, state.material_repository, state.space_service, embedder, dense)
        a = builder.build(space['id'], uploaded.version.id)
        b1 = builder.build_tree(a)
        assert b1['b1_ready']
        builder.publish(b1, expected_generation=0)
        chunks = repo.list_chunks(b1['retrieval_version_id'])
        allowed = chunks[:1]
        found = dense.search(embedder.embed(['学校'])[0], allowed)
        assert [row[0] for row in found] == [allowed[0]['chunk_id']]
        dense.delete_retrieval_version(a['retrieval_version_id'])
        assert client.count(dense.collection, exact=True).count == 0
    finally:
        if client.collection_exists(dense.collection):
            client.delete_collection(dense.collection)
        client.close()


def test_real_model_embedding_rerank_generation_and_verification(build_workspace, monkeypatch, record_property):
    if os.getenv('RAG_TEST_REAL_MODELS') != '1':
        pytest.skip('requires explicit real model opt-in')
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.providers.models import embedding_model
    settings = LearningSettings.from_env()
    state, uploaded, space, repo, _, _, _ = build_workspace
    uploaded = state.material_service.create(filename='live-folded-source.md',
        content=('# 范围\n\n学校应当告知监护人。\n\n保护未成年人，应当坚持最有利于未成年人的原\n则。').encode('utf-8'),
        idempotency_key='live-folded-source')
    space = state.create_space(dict(name='Live folded source', material_ids=[uploaded.material.id]))
    embedder = embedding_model(settings)
    url = os.getenv('RAG_TEST_QDRANT_URL')
    client = QdrantClient(url=url, timeout=20, check_compatibility=False) if url else QdrantClient(':memory:')
    prefix = 'knowpath_rag_smoke_' + uuid4().hex[:16]
    dense = QdrantContentIndex(client, prefix, embedder.dimension,
        {'provider': settings.embedding_provider, 'model': embedder.model_version,
         'dimension': embedder.dimension, 'endpoint': settings.embedding_base_url})
    try:
        builder = RagBuilder(repo, state.material_repository, state.space_service, embedder, dense)
        manifest = builder.build_tree(builder.build(space['id'], uploaded.version.id))
        builder.publish(manifest, expected_generation=0)
        # The live profile requires a dedicated real vector service. Exercise
        # both production runtimes through durable message publication and the
        # public citation resolver, including their actual provider journals.
        assert url, 'real model acceptance requires isolated Qdrant'
        from fastapi.testclient import TestClient
        from knowpath_backend.learning.api.application import create_app
        from knowpath_backend.learning.rag.runtime import ConfiguredPipeline
        monkeypatch.setenv('QDRANT_URL', url)
        monkeypatch.setenv('RAG_COLLECTION_PREFIX', prefix)
        receipts = []
        for mode in ('a', 'b1'):
            runtime = ConfiguredPipeline(state.material_repository, state.space_service, settings, mode)
            state.message_service.rag_pipeline = runtime
            sent = state.send_message(space['id'], {'message': '根据材料，学校应当告知谁？保护未成年人应当坚持什么原则？'})
            run = state.get_run(sent['run_id'])
            assert run['status'] == 'succeeded', run.get('error')
            completed = next(event['data'] for event in run['events'] if event['event'] == 'message.completed')
            assert completed['answer_status'] == 'answered'
            assert completed['citations']
            stored = state.message_service.repository.get_record('messages', completed['message_id'])
            trace = stored['snapshot']['rag_trace']
            journal = trace['call_journal']
            assert journal['status'] == 'succeeded'
            stages = {call['stage'] for call in journal['calls']}
            assert {'embedding', 'reranking', 'generation', 'verification'} <= stages
            assert all(call['usage'] for call in journal['calls']
                       if call['stage'] in {'generation', 'verification'})
            assert 1 <= trace['generation_calls'] <= 2
            assert 1 <= trace['verification_calls'] <= 2
            app = create_app(state.material_service, rag_pipeline=runtime)
            resolved_texts = []
            with TestClient(app) as api:
                api.headers['Idempotency-Key'] = 'live-source-' + mode
                route = f"/api/v1/learning-spaces/{space['id']}/citations/resolve"
                for citation in completed['citations']:
                    resolved = api.post(route, json=citation)
                    assert resolved.status_code == 200
                    resolved_texts.append(resolved.json()['text'])
                invalid = {**completed['citations'][0], 'retrieval_version_id': 'wrong'}
                assert api.post(route, json=invalid).status_code == 409
            assert '监护人' in ''.join(resolved_texts)
            assert '未成年人的原\n则' in ''.join(resolved_texts)
            receipts.append({'mode': mode, 'answer_status': completed['answer_status'],
                             'citation_resolved': True, 'call_journal': journal})
        record_property('real_chain', json.dumps(receipts, ensure_ascii=False))
    finally:
        if client.collection_exists(dense.collection):
            client.delete_collection(dense.collection)
        client.close()
