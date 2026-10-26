"""Memory interfaces backed by an explicitly active business unit of work.

These are persistence adapters, never candidate stores. Runtime assembly must
clone state into request-local backends before calling the agent or maintenance.
"""
import math
from knowpath_backend.memory.backends.base import ColdBackend, VectorBackend
from knowpath_backend.memory.item import MemoryItem
from knowpath_backend.learning.agent_memory.repository import consistent_read


class RepositoryColdBackend(ColdBackend):
    def __init__(self, repository): self.repository = repository
    def add(self, item):
        self.repository.update_item(item)
    @consistent_read
    def get(self, item_id): return self.repository.get_item(item_id)
    @consistent_read
    def iter_all(self): return iter(self.repository.list_items())
    @consistent_read
    def items_for_turn(self, turn_id): return self.repository.items_for_turn(turn_id)


class RepositoryVectorBackend(VectorBackend):
    def __init__(self, repository, *, model='default', dimension=0, predicate=None, scope_filters=None, scan_limit=1000):
        if type(scan_limit) is not int or scan_limit < 1: raise ValueError('Invalid vector scan capacity')
        self.repository, self.model, self.dimension = repository, model, dimension
        self.predicate, self.scan_limit = predicate or (lambda item: True), scan_limit
        self.scope_filters = dict(scope_filters or {})
        if not set(self.scope_filters) <= {'scope_hash', 'source_version', 'message_id'}: raise ValueError('Unsupported SQL scope filter')
        self.last_scan_count = 0
    def add(self, item, vector=None):
        self.repository._write()
        RepositoryColdBackend(self.repository).add(item)
        if vector is None: return
        identity = self.repository.index_identity(item.id, model=self.model, dimension=self.dimension)
        self.repository.complete_index(identity, vector)
    @consistent_read
    def _candidates(self):
        result = []
        rows = self.repository._rows('items', _limit=self.scan_limit + 1, embedding_model=self.model,
            embedding_dimension=self.dimension, lifecycle='active', **self.scope_filters)
        self.last_scan_count = len(rows)
        if len(rows) > self.scan_limit: raise ValueError('Vector scan capacity exceeded')
        for row in rows:
            item = self.repository._valid_item(row)
            if item is not None and row['vector'] is not None and self.predicate(item):
                result.append((item, row['vector']))
        return result
    @consistent_read
    def get(self, item_id):
        row = self.repository._one('items', item_id=item_id)
        if not row or row['embedding_model'] not in (None, self.model) or (row['embedding_model'] is not None and row['embedding_dimension'] != self.dimension): return None
        if any(row.get(key) != value for key, value in self.scope_filters.items()): return None
        item = self.repository._valid_item(row)
        return item if item is not None and self.predicate(item) else None
    @consistent_read
    def items(self): return [item for item, _ in self._candidates()]
    def remove(self, item_id):
        self.repository._write()
        item = self.get(item_id)
        if item is None: return None
        row = self.repository._one('items', item_id=item_id)
        if row and row['embedding_model'] == self.model and row['embedding_dimension'] == self.dimension and row['vector'] is not None:
            self.repository._put('items', dict(row, vector=None, index_generation=row['index_generation'] + 1))
            return item
        return None
    @consistent_read
    def search(self, query, query_vector=None, k=5):
        if query_vector is None: return []
        if (type(k) is not int or k < 0 or len(query_vector) != self.dimension
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in query_vector)):
            raise ValueError('Invalid query vector or capacity')
        norm = math.hypot(*query_vector)
        if not norm: return []
        scored = []
        for item, vector in self._candidates():
            if len(vector) != self.dimension or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector): continue
            vector_norm = math.hypot(*vector)
            if vector_norm: scored.append((item, sum((a/norm)*(b/vector_norm) for a, b in zip(query_vector, vector))))
        return sorted(scored, key=lambda pair: (-pair[1], pair[0].id))[:k]


class RepositoryPlaybookStore:
    def __init__(self, repository): self.repository = repository
    def add(self, card): self.repository.put_card(card)
    def update(self, card): self.add(card)
    @consistent_read
    def get(self, card_id): return self.repository.get_card(card_id)
    def delete(self, card_id): self.repository.delete_card(card_id)
    def remove(self, card_id): self.delete(card_id)
    @consistent_read
    def all_active(self):
        order = {'hard_constraint': 0, 'identity': 1, 'preference': 2, 'state': 3}
        return sorted((c for c in self.repository.list_cards() if c.is_active()), key=lambda c: (order[c.type.value], -c.confidence))
    @consistent_read
    def all_with_lifecycle(self, lifecycle): return [c for c in self.repository.list_cards() if c.lifecycle == lifecycle]
    @consistent_read
    def find_by_source_ref(self, source_ref): return [c for c in self.repository.list_cards() if c.source_ref == source_ref]
    @consistent_read
    def count(self): return len(self.repository.list_cards())
    @consistent_read
    def count_active(self): return len(self.all_active())
