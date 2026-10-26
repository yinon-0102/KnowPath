import importlib
import json
from copy import deepcopy

import pytest

from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
from knowpath_backend.learning.rag.evidence import PreparedEvidence, EvidenceRegistry
from knowpath_backend.memory import MemoryConfig, MemoryManager
from knowpath_backend.memory.item import MemoryItem
from knowpath_backend.test.test_web_agent_memory_scope import frozen, policy


def api():
    return importlib.import_module('knowpath_backend.learning.agent_runtime.tools')


@pytest.fixture
def memory():
    manager = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off'), context_policy=WebContextPolicy())
    yield manager
    manager.close()


def setup(memory, *, gate=None, prepare=None, registry=None):
    consumed = api().ConsumedDependencies(frozen(), 'current-message')
    tools = api().build_web_tools(memory=memory, policy=gate or policy(), current_receipt=frozen(),
        consumed=consumed, prepare_evidence=prepare or (lambda query: None), evidence_registry=registry)
    return tools, consumed


def item(identifier, text, *, allowed=True):
    receipt = frozen()
    if not allowed:
        receipt['space_id'] = 'another-space'
    return MemoryItem(id=identifier, content=text, role='user', metadata={
        'memory_scope': receipt,
        'source_dependencies': [{'material_id': 'a', 'material_version_id': 'a-v1'}]})


def test_scoped_recall_before_rank_preserves_heat_and_l3(memory):
    for index in range(20):
        memory.semantic.add(item('blocked-' + str(index), 'needle needle needle', allowed=False))
    allowed = item('allowed', 'needle other words')
    memory.semantic.add(allowed)
    tools, consumed = setup(memory)
    before = allowed.access_count
    result = json.loads(tools.execute_tool('recall', {'query': 'needle', 'k': 1}))
    assert [row['id'] for row in result['memories']] == ['allowed']
    assert allowed.access_count > before
    assert [entry.item_id for entry in memory.recall_buffer.entries()] == ['allowed']
    assert {'item_id': 'allowed'} in consumed.dependencies
    assert len(memory.semantic.items()) == 21


def test_remember_inherits_complete_consumed_graph_without_user_pin(memory):
    tools, consumed = setup(memory)
    consumed.consume_memory(item('old', 'past source'))
    result = json.loads(tools.execute_tool('remember', {'content': 'User studies mathematics'}))
    card = memory.playbook.get(result['id'])
    assert not card.user_pinned
    assert card.metadata['memory_scope'] == frozen()
    assert {'message_id': 'current-message'} in card.metadata['source_dependencies']
    assert {'material_id': 'a', 'material_version_id': 'a-v1'} in card.metadata['source_dependencies']
    assert {'item_id': 'old'} in card.metadata['source_dependencies']
    assert policy().eligible(card, purpose='L0')


@pytest.mark.parametrize('name,args', [
    ('remember', {'content': 'x', 'source_dependencies': []}),
    ('remember', {'content': 'x', 'pin': True}),
    ('recall', {'query': 'x', 'k': True}),
    ('search', {'query': 'x', 'material_ids': ['other']}),
])
def test_strict_tool_inputs_cannot_supply_authority(memory, name, args):
    tools, _ = setup(memory)
    with pytest.raises(ValueError):
        tools.execute_tool(name, args)
    assert not memory.playbook.all_active()


def test_search_retains_originals_and_collects_complete_span_dependencies(memory):
    source = dict(material_id='a', material_version_id='a-v1', chunk_id='chunk',
        retrieval_version_id='r1', citation_schema_version=2, source_text='x' * 10000,
        source_spans=[dict(material_version_id='a-v1', artifact_hash='a' * 64, block='b1', start=0, end=10000)])
    prepared = PreparedEvidence('ready', 'query', [source], {}, None, 0, 0)
    registry = EvidenceRegistry()
    tools, consumed = setup(memory, prepare=lambda query: prepared, registry=registry)
    result = json.loads(tools.execute_tool('search', {'query': 'query'}))
    assert result['evidence'][0]['id'] == 'E1'
    assert len(result['evidence'][0]['text']) <= 2000
    assert registry.resolve(['E1']) == [source]
    assert any(dep.get('artifact_hash') == 'a' * 64 and dep.get('end') == 10000 for dep in consumed.dependencies)
    prepared.sources[0]['source_text'] = 'mutated'
    assert len(registry.resolve(['E1'])[0]['source_text']) == 10000


def test_collector_receipt_and_dependencies_are_defensive_copies():
    receipt = frozen()
    consumed = api().ConsumedDependencies(receipt, 'm')
    original = deepcopy(receipt)
    receipt['space_id'] = 'forged'
    metadata = consumed.metadata()
    assert metadata['memory_scope'] == original
    metadata['source_dependencies'].clear()
    assert consumed.dependencies == [{'message_id': 'm'}]


def test_tools_are_serial_and_schemas_deny_extra_parameters(memory):
    tools, _ = setup(memory)
    assert set(tools.list_tools()) == {'recall', 'remember', 'search'}
    for name in tools.list_tools():
        assert tools.get_tool(name).side_effect_free is False
    for schema in tools.to_openai_schemas():
        assert schema['function']['parameters']['additionalProperties'] is False


def test_collector_expands_external_graph_instead_of_preview():
    value = item('original', 'history')
    preview = value.metadata['source_dependencies'][0]
    full = [preview, {'chunk_id': 'outside-preview', 'material_id': 'a', 'material_version_id': 'a-v1'}]
    value.metadata['source_ref'] = 'dependencies:full'
    consumed = api().ConsumedDependencies(frozen(), 'm', dependency_sets=lambda key: full)
    consumed.consume_memory(value)
    assert full[1] in consumed.dependencies


def test_recall_rechecks_tombstone_before_any_hit_mutation(memory):
    dead = item('deleted', 'needle')
    memory.semantic.add(dead)
    gate = policy()
    gate.tombstone_checker = lambda kind, identifier: kind == 'item' and identifier == 'deleted'
    tools, consumed = setup(memory, gate=gate)
    before = dead.access_count
    assert json.loads(tools.execute_tool('recall', {'query': 'needle'}))['memories'] == []
    assert dead.access_count == before
    assert not memory.recall_buffer.entries()
    assert consumed.dependencies == [{'message_id': 'current-message'}]


def test_search_rejects_scope_tampering_from_prepare(memory):
    source = dict(material_id='b', material_version_id='b-v1', chunk_id='chunk', source_text='unauthorized')
    prepared = PreparedEvidence('ready', 'query', [source], {}, None, 0, 0)
    registry = EvidenceRegistry()
    tools, consumed = setup(memory, prepare=lambda query: prepared, registry=registry)
    with pytest.raises(ValueError, match='eligible'):
        tools.execute_tool('search', {'query': 'query'})
    assert consumed.dependencies == [{'message_id': 'current-message'}]
    from knowpath_backend.learning.rag.verification import VerificationError
    with pytest.raises(VerificationError):
        registry.resolve(['E1'])
