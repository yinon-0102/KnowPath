"""Opt-in real services: unique collection and isolated SQL fixture only."""
import os
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


def test_real_model_embedding_rerank_generation_and_verification(build_workspace, monkeypatch):
    if os.getenv('RAG_TEST_REAL_MODELS') != '1':
        pytest.skip('requires explicit real model opt-in')
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.providers.models import embedding_model
    settings = LearningSettings.from_env()
    state, uploaded, space, repo, _, _, _ = build_workspace
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
        for plugin in (OrdinaryPlugin, TreePlugin):
            pipeline = RagPipeline(repo, state.material_repository, state.space_service, plugin(embedder, dense),
                DashScopeReranker(settings), AnswerVerifier(BudgetedJsonModel(settings), BudgetedJsonModel(settings)),
                require_b1=plugin is TreePlugin)
            result = pipeline.answer('根据材料，学校应当告知谁？', space_id=space['id'])
            assert result['status'] in {'answered', 'partial'}
            assert '监护人' in result['text'] and result['citations']
            assert 1 <= result['trace']['verification_calls'] <= 2
            assert result['trace']['usage']
        if url:
            from knowpath_backend.learning.rag.runtime import ConfiguredPipeline
            monkeypatch.setenv('QDRANT_URL', url)
            monkeypatch.setenv('RAG_COLLECTION_PREFIX', prefix)
            runtime = ConfiguredPipeline(state.material_repository, state.space_service, settings, 'a')
            result = runtime.answer('学校应当告知谁？', space_id=space['id'])
            assert result['status'] == 'answered' and result['citations']
    finally:
        if client.collection_exists(dense.collection):
            client.delete_collection(dense.collection)
        client.close()
