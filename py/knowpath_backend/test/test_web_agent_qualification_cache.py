"""Qualification memoization is scoped to one complete evaluation, never a turn."""
from collections import Counter
from copy import deepcopy
from uuid import uuid4

import pytest

from knowpath_backend.test.test_learning_state_persistence import workspace


def chain(workspace, length=8):
    factory, sid, _, _ = workspace
    state = factory()
    state.send_message(sid, {'message': 'Explain functions'})
    service = state.message_service
    business = service.repository
    original = business.records('messages', space_id=sid)[0]
    ancestors = [original['id']]
    for sequence in range(2, length + 1):
        row = deepcopy(original)
        row.update(id=str(uuid4()), sequence=sequence, message=f'Synthetic chained question {sequence}')
        row['snapshot']['source_dependencies'] = [{'message_id': identifier} for identifier in ancestors]
        with business.transaction():
            business.put_record('messages', row)
        ancestors.append(row['id'])
    repo = business.agent_memory_factory(sid)
    gate = service._build_memory_policy(repo, original['snapshot']['memory_scope'], service.spaces.materials)
    candidate = {'id': 'synthetic-derived', 'metadata': {
        'memory_scope': original['snapshot']['memory_scope'],
        'source_dependencies': [{'message_id': identifier} for identifier in ancestors]}}
    return service, repo, gate, candidate, ancestors


def test_complete_ancestor_graph_reads_each_reference_once_per_evaluation(workspace, monkeypatch):
    service, repo, gate, candidate, ancestors = chain(workspace)
    reads = Counter()
    original = repo._business_message
    def counted(identifier):
        reads[identifier] += 1
        return original(identifier)
    monkeypatch.setattr(repo, '_business_message', counted)
    assert gate.eligible(candidate, purpose='recall')
    assert reads == Counter({identifier: 1 for identifier in ancestors})
    reads.clear()
    assert gate.eligible(candidate, purpose='recall')
    assert reads == Counter({identifier: 1 for identifier in ancestors})


def test_same_policy_rechecks_revoked_ancestor_after_success(workspace):
    service, repo, gate, candidate, ancestors = chain(workspace, 4)
    assert gate.eligible(candidate, purpose='recall')
    with service.repository.transaction():
        source = service.repository.get_record('messages', ancestors[0])
        source['status'] = 'failed'
        service.repository.put_record('messages', source)
    assert not gate.eligible(candidate, purpose='recall')


def test_reference_cycle_is_denied_and_does_not_poison_later_evaluation(workspace):
    service, repo, gate, candidate, ancestors = chain(workspace, 2)
    with service.repository.transaction():
        source = service.repository.get_record('messages', ancestors[0])
        saved = deepcopy(source['snapshot'])
        source['snapshot']['source_dependencies'] = [{'message_id': ancestors[-1]}]
        service.repository.put_record('messages', source)
    assert not gate.eligible(candidate, purpose='recall')
    with service.repository.transaction():
        source['snapshot'] = saved
        service.repository.put_record('messages', source)
    assert gate.eligible(candidate, purpose='recall')
