"""L5 语义索引：薄包装层，把请求委托给具体的 VectorBackend。

按检索时机的不同：
- 没有 embedder + 内存后端 → TF-IDF
- 有 embedder + 内存后端 → 余弦相似度
- SQLite 后端 → 同上但带持久化（重启可恢复）
"""
from typing import List, Optional, Tuple

from knowpath_backend.memory.backends.base import VectorBackend
from knowpath_backend.memory.backends.inmemory import InMemoryVectorBackend
from knowpath_backend.memory.base import MemoryTier
from knowpath_backend.memory.item import MemoryItem


class SemanticIndex(MemoryTier):
    """L5 —— 语义检索层。"""

    name = "L5"

    def __init__(self, backend: Optional[VectorBackend] = None):
        self._hot_items = {}
        self.backend = backend if backend is not None else InMemoryVectorBackend()

    def add(self, item: MemoryItem) -> None:
        self.backend.add(item)

    def get(self, item_id: str) -> Optional[MemoryItem]:
        return self._hot_items.get(item_id) or self.backend.get(item_id)

    def items(self) -> List[MemoryItem]:
        return [self._hot_items.get(item.id, item) for item in self.backend.items()]

    def remove(self, item_id: str) -> Optional[MemoryItem]:
        self._hot_items.pop(item_id, None)
        return self.backend.remove(item_id)

    def search(self, query: str, k: int = 5) -> List[Tuple[MemoryItem, float]]:
        return [(self._hot_items.get(item.id, item), score) for item, score in self.backend.search(query, k=k)]

    def bind_hot_items(self, items):
        """Overlay validated session objects without writes or embedding calls."""
        self._hot_items = {item.id: item for item in items}
