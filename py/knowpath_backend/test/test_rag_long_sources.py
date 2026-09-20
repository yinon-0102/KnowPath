"""Legacy compatibility never silently indexes or cites a truncated source."""
import pytest
from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.rag.indexing import index_material_version
from knowpath_backend.learning.conversations.context import bound_snapshot
from knowpath_backend.learning.conversations.generation import MessageGenerationError


def test_source_and_legacy_index_keep_tail_for_explicit_budget_decision():
    state = LearningState()
    content = '# 长文\n' + '甲' * 6100 + '必须事先获得同意。'
    result = state.material_service.create(filename='long.md', content=content.encode(), idempotency_key='long')
    space = state.create_space({'name':'长文', 'material_ids':[result.material.id]})
    sources = state.message_service._sources(space)
    assert sources[0]['text'].endswith('必须事先获得同意。')
    class Indexer:
        def index(self, rows):
            assert rows[0]['text'].endswith('必须事先获得同意。')
            return {'indexed_chunks': len(rows)}
    index_material_version(state.material_repository, Indexer(), result.version.id)


def test_legacy_context_cannot_cut_away_tail_condition():
    source = 'Allowed action ' * 8000 + 'only with consent'
    with pytest.raises(MessageGenerationError, match='CONTEXT_BUDGET_EXCEEDED'):
        bound_snapshot({'message':'条件？', 'sources':[{'chunk_id':'x','text':source}], 'history':[]}, budget=1800)
