"""One prerequisite contract for scope writes and plan ordering."""
from hashlib import sha256
import json


def canonical(topic):
    return topic.get('canonical_topic_id') or topic['id']


def effective(topic):
    return topic.get('status', 'active') == 'active' and topic.get('is_learnable', True)


def expand_selection(identifiers, topics):
    """Containers select learnable descendants, including direct introductions."""
    by_id = {t['id']: t for t in topics}
    result, seen = [], set()
    def add(identifier, visiting):
        if identifier in visiting or identifier not in by_id:
            return
        topic = by_id[identifier]
        if effective(topic):
            owner = canonical(topic) if 'structure_key' in topic else identifier
            identity = canonical(topic) if 'structure_key' in topic else identifier
            if identity not in seen:
                seen.add(identity)
                result.append(owner)
        elif topic.get('is_learnable') is False:
            for child in topics:
                if child.get('parent_id') == canonical(topic) and child['id'] == canonical(child):
                    add(child['id'], visiting | {identifier})
    for identifier in identifiers:
        add(identifier, set())
    return result


def mastered(topic, states, topics):
    """Only current, valid mastery for this precise knowledge revision suffices."""
    topic = next((t for t in topics if t['id'] == canonical(topic)), topic)
    current = topic.get('learning_revision_id') or sha256(json.dumps(topic.get('source_refs', []), sort_keys=True).encode()).hexdigest()
    identifiers = {t['id'] for t in topics if canonical(t) == canonical(topic)}
    return any(row.get('status') == 'mastered' and row.get('score_validity') == 'current'
               and row.get('topic_revision_id') == current and not row.get('review_due', False)
               for identifier, row in states.items() if identifier in identifiers)


def prerequisite_conflicts(selected, topics, states):
    by_id = {t['id']: t for t in topics}
    chosen = {canonical(t) for t in selected}
    conflicts = []
    for topic in selected:
        for identifier in topic.get('prerequisites', []):
            parent = by_id.get(identifier)
            if parent and (canonical(parent) in chosen or mastered(parent, states, topics)):
                continue
            conflicts.append({'topic_id': topic['id'], 'topic_name': topic.get('name', topic['id']),
                              'prerequisite_topic_id': identifier,
                              'prerequisite_name': (parent or {}).get('name', identifier),
                              'reason': '未掌握的必要前置知识未纳入学习范围'})
    return conflicts
