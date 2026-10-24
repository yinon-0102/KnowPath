"""L1 工作内存：永驻 context、带 token 上限、最近 N 条受保护。"""
from typing import List, Optional

from knowpath_backend.memory.base import MemoryTier
from knowpath_backend.memory.config import MemoryConfig
from knowpath_backend.memory.item import MemoryItem


class WorkingMemory(MemoryTier):
    """L1 —— 每次 LLM 调用都会被纳入 context 的核心区域。

    驱逐规则：
    1. token 总量超过 l1_max_tokens 时触发
    2. 最近 l1_recent_turns 条永远保留（避免破坏当前对话上下文）
    3. pinned 项不驱逐
    4. 其余按 FIFO 驱逐最老的
    """

    name = "L1"

    def __init__(self, config: MemoryConfig, *, whole_turns: bool = False):
        self.whole_turns = whole_turns
        self.config = config
        self._items: List[MemoryItem] = []

    def add(self, item: MemoryItem) -> None:
        self._items.append(item)

    def get(self, item_id: str) -> Optional[MemoryItem]:
        for it in self._items:
            if it.id == item_id:
                return it
        return None

    def items(self) -> List[MemoryItem]:
        return list(self._items)

    def remove(self, item_id: str) -> Optional[MemoryItem]:
        for idx, it in enumerate(self._items):
            if it.id == item_id:
                return self._items.pop(idx)
        return None

    def total_tokens(self) -> int:
        return sum(it.token_estimate for it in self._items)

    def evict(self) -> List[MemoryItem]:
        """超 budget 时驱逐：跳过 pinned 和最近 N 条。"""
        if self.whole_turns:
            groups = self.turn_groups()
            complete = [group for group in groups if complete_turn(group) is not None]
            recent = complete[-self.config.l1_recent_turns:] if self.config.l1_recent_turns else []
            protected = {turn_key(group[0]) for group in recent}
            evicted = []
            for group in groups:
                if self.total_tokens() <= self.config.l1_max_tokens:
                    break
                if turn_key(group[0]) in protected or any(item.pinned for item in group):
                    continue
                for item in group:
                    self.remove(item.id)
                    evicted.append(item)
            return evicted
        evicted: List[MemoryItem] = []
        budget = self.config.l1_max_tokens
        recent_n = self.config.l1_recent_turns

        if self.total_tokens() <= budget:
            return evicted

        # 倒数 recent_n 条的起始索引 —— 这些不动
        protected_start = max(0, len(self._items) - recent_n)

        idx = 0
        while self.total_tokens() > budget and idx < protected_start:
            candidate = self._items[idx]
            if candidate.pinned:
                idx += 1
                continue
            evicted.append(self._items.pop(idx))
            protected_start -= 1  # 列表缩短，保护区起点跟着前移

        return evicted

    def turn_groups(self):
        return group_turns(self._items, infer_legacy=not self.whole_turns)


def group_turns(items, *, infer_legacy=True):
    """Stable first-seen turn order. Legacy untagged adjacent pairs stay compatible."""
    groups = []
    tagged = {}
    for item in items:
        key = turn_key(item)
        if key is not None:
            if key not in tagged:
                tagged[key] = []
                groups.append(tagged[key])
            tagged[key].append(item)
        elif infer_legacy and groups and not turn_key(groups[-1][0]) and item.role == 'assistant' and groups[-1][-1].role == 'user':
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def turn_key(item):
    """Only an explicit stable tag establishes Web turn identity."""
    legacy_tag = item.metadata.get('turn_id')
    if item.turn_id is not None and legacy_tag is not None and item.turn_id != legacy_tag:
        return None
    tag = item.turn_id if item.turn_id is not None else legacy_tag
    return tag if type(tag) is str and tag else None


def complete_turn(group):
    """One active user and one active assistant with the same explicit turn ID."""
    if (len(group) != 2 or {item.role for item in group} != {'user', 'assistant'}
            or len({item.id for item in group}) != 2 or any(not item.is_active for item in group)):
        return None
    key = turn_key(group[0])
    if key is None or any(turn_key(item) != key for item in group):
        return None
    return sorted(group, key=lambda item: item.role == 'assistant')
