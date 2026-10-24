"""Authorization changes the candidate corpus before scoring, not after top-k."""
import pytest
from knowpath_backend.memory import MemoryConfig, MemoryManager
from knowpath_backend.memory.backends.inmemory import InMemoryVectorBackend
from knowpath_backend.memory.item import MemoryItem


@pytest.mark.parametrize('dense', [False, True])
def test_filtered_backend_retains_vectors_and_excludes_before_rank(dense):
    backend = InMemoryVectorBackend()
    allowed = MemoryItem(content='alpha beta')
    denied = [MemoryItem(content='alpha alpha alpha') for _ in range(20)]
    backend.add(allowed, vector=[0.9, 0.1] if dense else None)
    for item in denied:
        backend.add(item, vector=[1.0, 0.0] if dense else None)
    before = dict(backend._embeddings)
    hits = backend.search('alpha', query_vector=[1.0, 0.0] if dense else None,
                          k=1, candidate_filter=lambda item: item.id == allowed.id)
    assert [item.id for item, score in hits] == [allowed.id]
    assert len(backend.items()) == 21
    assert backend._embeddings == before
    if not dense:
        assert hits[0][1] == 1.0  # IDF computed only over the authorized corpus.


def test_manager_filtered_recall_touches_only_authorized_item():
    manager = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off'))
    try:
        permitted = MemoryItem(content='alpha beta')
        forbidden = MemoryItem(content='alpha alpha alpha')
        manager.semantic.add(permitted)
        manager.semantic.add(forbidden)
        result = manager.recall('alpha', k=1, candidate_filter=lambda item: item.id == permitted.id)
        assert [item.id for item, _ in result] == [permitted.id]
        assert permitted.access_count == 1
        assert forbidden.access_count == 0
    finally:
        manager.close()


def test_context_filter_runs_before_l0_refresh_and_relevance():
    manager = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off'))
    try:
        denied = manager.remember('private forbidden fact')
        before = denied.model_dump(mode='json')
        def relevance(text, query):
            assert 'private forbidden' not in text
            return 1.0
        manager._context_engine.relevance_fn = relevance
        segments = manager._gather_segments('system', 'query', 0,
            candidate_filter=lambda value, purpose: False)
        assert [segment.source for segment in segments] == ['system']
        assert denied.model_dump(mode='json') == before
    finally:
        manager.close()
