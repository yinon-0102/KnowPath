import json
from concurrent.futures import Future
import pytest
from knowpath_backend.memory import MemoryConfig, MemoryManager
from knowpath_backend.memory.item import MemoryItem


def manager(**kwargs):
    return MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off', **kwargs))


def test_checkpoint_roundtrip_preserves_hot_state_without_cold_copy():
    a, b = manager(), manager()
    item = a.write('question', metadata={'turn_id': 't1', 'source_dependencies': [{'material_id': 'm'}]})
    a.write('answer', role='assistant', metadata={'turn_id': 't1'})
    item.touch()
    a.summary.add(item.model_copy(deep=True)); a.summary.flush()
    a.summary.add(MemoryItem(content='pending', metadata={'scope': ['m']}))
    a.recall_buffer.record_hit('cold-id', .8, 0)
    a.recall_buffer.record_hit('cold-id', .9, 1)
    a.tick(); a.tick()
    cp = a.export_checkpoint()
    b.restore_checkpoint(json.loads(json.dumps(cp.to_dict())))
    assert b.export_checkpoint() == cp
    assert set(cp.state) == {'l1', 'l2', 'l3', 'counters', 'whole_turns'}
    b.working.items()[0].metadata['source_dependencies'].append({'material_id': 'other'})
    assert len(a.working.items()[0].metadata['source_dependencies']) == 1
    assert b.recall_buffer.evict_expired(5) == ['cold-id']
    a.close(); b.close()


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), object()])
def test_checkpoint_rejects_non_json_state(bad):
    a = manager(); a.write('x', metadata={'bad': bad})
    with pytest.raises((ValueError, TypeError)):
        a.export_checkpoint()
    a.close()


def test_checkpoint_unknown_version_and_restore_are_atomic():
    a = manager(); a.write('keep')
    cp = a.export_checkpoint().to_dict()
    cp['schema_version'] = 999
    with pytest.raises(ValueError): a.restore_checkpoint(cp)
    assert [it.content for it in a.working.items()] == ['keep']
    a.close()


def test_checkpoint_rejects_inflight_tick_without_waiting_on_own_lock():
    a = manager(); a._pending_tick = Future()
    with pytest.raises(RuntimeError, match='tick'): a.export_checkpoint()
    a._pending_tick.set_result({}); a.close()


def test_summary_and_l0_promotion_preserve_dependencies():
    a = manager()
    md = {'source_dependencies': [{'material_id': 'm', 'version': 3}], 'scope': {'space_id': 's'}}
    it = a.write('我是程序员并且正在学习递归', metadata=md)
    a.summary.add(it); summary = a.summary.flush()
    assert summary.metadata['source_dependencies'] == md['source_dependencies']
    assert summary.metadata['source_item_ids'] == [it.id]
    card = a.playbook.all_active()[0]
    assert card.metadata['scope'] == md['scope']
    assert card.metadata['source_dependencies'] == md['source_dependencies']
    a.close()


def test_checkpoint_invalid_state_is_atomic_and_has_typed_error():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    a = manager(); a.write('keep')
    baseline = a.export_checkpoint()
    for mutate in [lambda cp: cp['state']['l1'].append(cp['state']['l1'][0]),
                   lambda cp: cp['state']['counters'].update(turn=-1),
                   lambda cp: cp['state']['l2'].update(buffer=[{'content': 'partial'}]),
                   lambda cp: cp['state']['l1'][0].update(extra_field=True),
                   lambda cp: cp['state']['l1'][0].update(access_count=-1)]:
        cp = baseline.to_dict(); mutate(cp)
        with pytest.raises(InvalidCheckpoint): a.restore_checkpoint(cp)
        assert a.export_checkpoint() == baseline
    a.close()


def test_restore_has_no_backend_writes_and_uses_same_hot_item_in_l1_l5(monkeypatch):
    a, b = manager(), manager()
    item = a.write('same content'); item.touch()
    b.semantic.add(item.model_copy(deep=True))
    monkeypatch.setattr(b.semantic.backend, 'add', lambda *args: pytest.fail('restore wrote backend'))
    b.restore_checkpoint(a.export_checkpoint())
    restored = b.working.items()[0]
    assert b.semantic.get(item.id) is restored
    restored.touch()
    assert b.semantic.items()[0].access_count == 2
    assert b.semantic.search('content')[0][0] is restored
    assert a.working.items()[0].access_count == 1
    a.close(); b.close()


def test_checkpoint_limits_fail_without_dropping_pending_sources():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    a = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off', checkpoint_max_bytes=1000, checkpoint_max_buffer_entries=1))
    a.summary.add(MemoryItem(content='a')); a.summary.add(MemoryItem(content='b'))
    with pytest.raises(InvalidCheckpoint): a.export_checkpoint()
    assert len(a.summary.pending_items()) == 2
    a.close()


def test_summary_dependencies_are_canonical_and_limit_is_explicit():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    a = manager()
    it = MemoryItem(content='x', metadata={'source_dependencies': [{'material_id': 'm', 'version': 3}], 'irrelevant': 'body' * 10000})
    a.summary.add(it); sm = a.summary.flush()
    assert 'source_records' not in sm.metadata
    assert sm.metadata['source_item_ids'] == [it.id]
    assert len(json.dumps(sm.metadata)) < 1000
    a.summary.max_dependencies = 1
    other = MemoryItem(content='y', metadata={'source_dependencies': [{'material_id': 'n'}]})
    a.summary.add(other)
    before = sm.model_copy(deep=True)
    with pytest.raises(InvalidCheckpoint): a.summary.flush()
    assert a.summary.current_summary() == before
    assert a.summary.pending_items() == [other]
    a.close()


def test_remember_and_reconcile_inherit_source_dependencies():
    a = manager()
    dep = {'material_id': 'm', 'version': 1}
    card = a.remember('fact', metadata={'source_dependencies': [dep]})
    assert card.metadata['source_dependencies'] == [dep]
    dep['version'] = 99
    assert card.metadata['source_dependencies'][0]['version'] == 1
    a.summary.add(MemoryItem(content='old')); a.summary.flush()
    a.summary.reconciler = lambda old, signal: 'new'
    source = MemoryItem(content='source', metadata={'source_dependencies': [{'material_id': 'n'}]})
    a.summary.reconcile('signal', sources=[source])
    assert a.summary.current_summary().metadata['source_dependencies'] == [{'material_id': 'n'}]
    a.close()


def test_checkpoint_busy_does_not_mutate_counter_when_async_tick_inflight(monkeypatch):
    import threading
    from knowpath_backend.memory.checkpoint import CheckpointBusy
    entered, release = threading.Event(), threading.Event()
    a = MemoryManager(MemoryConfig(tick_mode='async', conflict_strength='off'))
    original = a._tick_impl
    def blocked():
        entered.set(); release.wait(5); return original()
    monkeypatch.setattr(a, '_tick_impl', blocked)
    a.tick(); assert entered.wait(2)
    try:
        with pytest.raises(CheckpointBusy): a.export_checkpoint()
    finally:
        release.set(); a.wait_for_tick(5)
    assert a.export_checkpoint().state['counters']['turn'] == 1
    a.close()



def test_expired_l3_does_not_promote_on_tick_after_restart():
    from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
    cfg = MemoryConfig(tick_mode='sync', conflict_strength='off', l3_ttl_turns=1, l3_promote_min_hits=1)
    m = MemoryManager(cfg, context_policy=WebContextPolicy())
    for role in ['user', 'assistant']: m.write('old ' + role, role=role, turn_id='t')
    old = m.working.items()
    for item in old: m.working.remove(item.id)
    m.tick(); m.tick()
    m.recall_buffer.record_hit(old[0].id, .9, 0)
    m.tick()
    assert m.working.items() == []
    assert m.recall_buffer.entries() == []
    m.close()


def test_restore_rejects_indexed_body_drift_without_mutation():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    a, b = manager(), manager()
    item = a.write('original')
    b.semantic.add(item.model_copy(update={'content': 'changed'}, deep=True))
    before = b.export_checkpoint()
    with pytest.raises(InvalidCheckpoint): b.restore_checkpoint(a.export_checkpoint())
    assert b.export_checkpoint() == before
    assert b.semantic.get(item.id).content == 'changed'
    a.close(); b.close()


def test_restore_validates_summary_dependency_limits():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    a = manager(summary_max_dependencies=1)
    a.summary.add(MemoryItem(content='old')); a.summary.flush()
    cp = a.export_checkpoint().to_dict()
    cp['state']['l2']['summary']['metadata']['source_item_ids'] = ['first', 'second']
    with pytest.raises(InvalidCheckpoint): a.restore_checkpoint(cp)
    a.close()


def test_summary_rejects_noncanonical_dependency_without_losing_buffer():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    a = manager()
    item = MemoryItem(content='x', metadata={'source_dependencies': [{'body': 'source body'}]})
    a.summary.add(item)
    with pytest.raises(InvalidCheckpoint): a.summary.flush()
    assert a.summary.current_summary() is None
    assert a.summary.pending_items() == [item]
    a.close()


@pytest.mark.parametrize('field,value', [('turn_id', 'other-turn'), ('metadata', {'source_dependencies': [{'material_id': 'other'}]}), ('metadata', {'scope': {'space_id': 'other'}}), ('superseded_by', 'new-id')])
def test_restore_rejects_indexed_source_turn_drift_and_inactive_items(field, value):
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    a, b = manager(), manager()
    item = a.write('same original', turn_id='turn', metadata={'scope': {'space_id': 's'}, 'source_dependencies': [{'material_id': 'm'}]})
    b.semantic.add(item.model_copy(update={field: value}, deep=True))
    before = b.export_checkpoint()
    with pytest.raises(InvalidCheckpoint): b.restore_checkpoint(a.export_checkpoint())
    assert b.export_checkpoint() == before
    assert b.semantic.get(item.id).model_dump()[field] == value
    a.close(); b.close()


def test_web_restore_cannot_disable_whole_turn_policy():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
    generic = manager()
    web = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off'), context_policy=WebContextPolicy())
    with pytest.raises(InvalidCheckpoint): web.restore_checkpoint(generic.export_checkpoint())
    generic.close(); web.close()


def test_web_policy_rejects_async_tick_before_constructing_storage(monkeypatch):
    from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
    monkeypatch.setattr('knowpath_backend.memory.manager.PlaybookStore', lambda *args: pytest.fail('created store'))
    with pytest.raises(ValueError, match='async'):
        MemoryManager(MemoryConfig(tick_mode='async'), context_policy=WebContextPolicy())


def test_restore_interns_same_id_across_l1_pending_buffer_and_index():
    original, restored = manager(), manager()
    item = original.write('shared', metadata={'source_dependencies': [{'material_id': 'm'}]})
    original.summary.add(item)
    restored.semantic.add(item.model_copy(deep=True))
    restored.restore_checkpoint(original.export_checkpoint())
    live = restored.working.items()[0]
    assert restored.summary.pending_items()[0] is live
    assert restored.semantic.get(item.id) is live
    live.touch()
    assert restored.summary.pending_items()[0].access_count == 1
    assert item.access_count == 0
    original.close(); restored.close()


@pytest.mark.parametrize('change', [{'content': 'drift'}, {'turn_id': 'other'}, {'metadata': {'scope': 'other'}}])
def test_restore_prevalidates_pending_buffer_index_identity_atomically(change):
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    original, restored = manager(), manager()
    pending = MemoryItem(content='pending', turn_id='turn', metadata={'scope': {'space_id': 's'}})
    original.summary.add(pending)
    restored.semantic.add(pending.model_copy(update=change, deep=True))
    before = restored.export_checkpoint()
    with pytest.raises(InvalidCheckpoint): restored.restore_checkpoint(original.export_checkpoint())
    assert restored.export_checkpoint() == before
    assert restored.semantic.get(pending.id).model_dump() == pending.model_copy(update=change, deep=True).model_dump()
    original.close(); restored.close()


def test_restore_rejects_conflicting_same_id_inside_hot_layers_atomically():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    original, restored = manager(), manager()
    item = original.write('shared')
    original.summary.add(item)
    cp = original.export_checkpoint().to_dict()
    cp['state']['l2']['buffer'][0]['content'] = 'conflicting same ID'
    before = restored.export_checkpoint()
    with pytest.raises(InvalidCheckpoint): restored.restore_checkpoint(cp)
    assert restored.export_checkpoint() == before
    original.close(); restored.close()


def test_default_summary_long_source_free_history_keeps_legacy_behavior():
    from knowpath_backend.memory.summary import SummaryMemory
    summary = SummaryMemory(flush_threshold=1)
    for number in range(513): summary.add(MemoryItem(content='plain message ' + str(number)))
    assert summary.current_summary() is not None
    assert summary.current_summary().metadata == {'kind': 'summary'}


@pytest.mark.parametrize('promotion', ['seed', 'heat', 'remember'])
def test_web_l0_uses_canonical_source_metadata_and_discards_raw_history(promotion):
    from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
    m = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off'), context_policy=WebContextPolicy())
    metadata = {'source_dependencies': [{'material_id': 'm', 'version': 2}],
                'scope': {'space_id': 's'}, 'raw_history': 'secret' * 10000}
    if promotion == 'remember':
        card = m.remember('fact', metadata=metadata)
    elif promotion == 'seed':
        item = m.write('我是程序员，正在学习递归', role='user', turn_id='t', metadata=metadata)
        card = m.playbook.all_active()[0]
    else:
        item = m.write('plain', role='user', turn_id='t', metadata=metadata)
        item.pinned = True; item.access_count = m.config.l0_graduate_min_hits
        assert m._maybe_graduate_to_l0() == [item.id]
        card = m.playbook.all_active()[0]
    assert 'raw_history' not in card.metadata
    assert card.metadata['source_dependencies'] == metadata['source_dependencies']
    assert card.metadata['scope'] == metadata['scope']
    assert len(json.dumps(card.metadata)) < 2048
    m.close()


def test_hot_intern_merges_monotonic_heat_without_overwriting_l1_pin_priority():
    original, restored = manager(), manager()
    item = original.write('shared')
    original.summary.add(item)
    cp = original.export_checkpoint().to_dict()
    cp['state']['l2']['buffer'][0].update(access_count=9, prior_score=.8, pinned=True)
    restored.restore_checkpoint(cp)
    live = restored.working.items()[0]
    assert restored.summary.pending_items()[0] is live
    assert live.access_count == 9 and live.prior_score == .8
    assert live.pinned is False
    original.close(); restored.close()


def test_explicit_provenance_and_web_summary_keep_bounded_source_free_ids():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
    for kwargs in [{'provenance': True}, {'context_policy': WebContextPolicy()}]:
        m = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off', summary_max_dependencies=1), **kwargs)
        m.summary.add(MemoryItem(content='one')); m.summary.flush()
        m.summary.add(MemoryItem(content='two'))
        with pytest.raises(InvalidCheckpoint): m.summary.flush()
        assert len(m.summary.pending_items()) == 1
        m.close()


def test_web_remember_limit_failure_keeps_card_store_unchanged():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
    m = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off', summary_max_dependencies=1), context_policy=WebContextPolicy())
    with pytest.raises(InvalidCheckpoint):
        m.remember('fact', metadata={'source_dependencies': [{'material_id': 'a'}, {'material_id': 'b'}]})
    assert m.playbook.all_active() == []
    m.close()


@pytest.mark.parametrize('override', [{'whole_turns': False}, {'provenance': False}])
def test_web_strict_policy_cannot_be_disabled_before_storage_creation(monkeypatch, override):
    from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
    monkeypatch.setattr('knowpath_backend.memory.manager.PlaybookStore', lambda *args: pytest.fail('created store'))
    with pytest.raises(ValueError, match='policy'):
        MemoryManager(MemoryConfig(tick_mode='off'), context_policy=WebContextPolicy(), **override)


def test_restore_rejects_lifecycle_disagreement_between_hot_copies_atomically():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    original, restored = manager(), manager()
    item = original.write('same item')
    original.summary.add(item)
    cp = original.export_checkpoint().to_dict()
    cp['state']['l2']['buffer'][0]['superseded_by'] = 'replacement'
    before = restored.export_checkpoint()
    with pytest.raises(InvalidCheckpoint): restored.restore_checkpoint(cp)
    assert restored.export_checkpoint() == before
    original.close(); restored.close()


def test_restore_rejects_source_scopes_identity_drift_atomically():
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    original, restored = manager(), manager()
    item = original.write('same item', metadata={'source_scopes': [{'item_id': 'source', 'scope': {'space_id': 's'}}]})
    restored.semantic.add(item.model_copy(deep=True))
    cp = original.export_checkpoint().to_dict()
    cp['state']['l1'][0]['metadata']['source_scopes'][0]['scope']['space_id'] = 'other'
    before = restored.export_checkpoint()
    with pytest.raises(InvalidCheckpoint): restored.restore_checkpoint(cp)
    assert restored.export_checkpoint() == before
    assert restored.semantic.get(item.id).metadata['source_scopes'][0]['scope']['space_id'] == 's'
    original.close(); restored.close()


@pytest.mark.parametrize('operation', ['flush', 'reconcile'])
def test_summary_keeps_transitive_derived_source_ids_and_scopes(operation):
    from knowpath_backend.memory.summary import SummaryMemory
    scope = {'item_id': 'original-message', 'scope': {'space_id': 's', 'material_ids': ['m']}}
    source = MemoryItem(content='derived source', metadata={
        'source_item_ids': ['original-message'],
        'source_scopes': [scope],
        'source_dependencies': [{'material_id': 'm', 'version': 2}],
        'scope': {'space_id': 's'},
    })
    summary = SummaryMemory(track_provenance=True, reconciler=lambda old, signal: 'reconciled')
    if operation == 'flush':
        summary.add(source); result = summary.flush()
    else:
        summary.add(MemoryItem(content='old')); summary.flush()
        result = summary.reconcile('derived change', sources=[source])
    assert source.id in result.metadata['source_item_ids']
    assert 'original-message' in result.metadata['source_item_ids']
    assert scope in result.metadata['source_scopes']
    assert {'item_id': source.id, 'scope': {'scope': {'space_id': 's'}}} in result.metadata['source_scopes']
    assert result.metadata['source_dependencies'] == [{'material_id': 'm', 'version': 2}]
    source.metadata['source_scopes'][0]['scope']['material_ids'].append('other')
    assert result.metadata['source_scopes'][0]['scope']['material_ids'] == ['m']


def test_direct_dependency_merge_preserves_and_deduplicates_transitive_sources():
    from knowpath_backend.memory.dependencies import merge_sources
    scope = {'item_id': 'original', 'scope': {'space_id': 's'}}
    source = MemoryItem(content='derived', metadata={'source_item_ids': ['original', 'original'], 'source_scopes': [scope, scope]})
    result = merge_sources({}, [source, source])
    assert result['source_item_ids'] == [source.id, 'original']
    assert result['source_scopes'] == [scope]
    source.metadata['source_scopes'][0]['scope']['space_id'] = 'changed'
    assert result['source_scopes'][0]['scope']['space_id'] == 's'


@pytest.mark.parametrize('operation', ['flush', 'reconcile'])
@pytest.mark.parametrize('inherited', [
    {'source_item_ids': ['one', 'two']},
    {'source_item_ids': 'not-a-list'},
    {'source_item_ids': [42]},
    {'source_scopes': 'not-a-list'},
    {'source_scopes': [{'item_id': 'original', 'scope': {'space_id': 's'}, 'unknown': 'field'}]},
])
def test_invalid_transitive_sources_fail_before_summary_or_buffer_changes(operation, inherited):
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    from knowpath_backend.memory.summary import SummaryMemory
    calls = []
    summary = SummaryMemory(track_provenance=True, max_dependencies=2,
                            summarizer=lambda items, previous: calls.append('flush') or 'new',
                            reconciler=lambda previous, signal: calls.append('reconcile') or 'changed')
    summary.add(MemoryItem(content='old')); summary.flush()
    before = summary.current_summary().model_copy(deep=True)
    source = MemoryItem(content='derived', metadata=inherited)
    calls.clear()
    if operation == 'flush':
        summary.add(source)
        with pytest.raises(InvalidCheckpoint): summary.flush()
        assert summary.pending_items() == [source]
    else:
        with pytest.raises(InvalidCheckpoint): summary.reconcile('change', sources=[source])
        assert summary.pending_items() == []
    assert summary.current_summary() == before
    assert calls == []


@pytest.mark.parametrize('operation', ['flush', 'reconcile'])
@pytest.mark.parametrize('payload', [None, 42, 'invalid-scope', {'raw_history': 'source body'}, {'scope': {'raw_history': 'nested body'}}])
def test_noncanonical_scope_payload_is_rejected_before_summary_callbacks(operation, payload):
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    from knowpath_backend.memory.summary import SummaryMemory
    calls = []
    summary = SummaryMemory(track_provenance=True,
                            summarizer=lambda items, previous: calls.append('flush') or 'new',
                            reconciler=lambda previous, signal: calls.append('reconcile') or 'changed')
    summary.add(MemoryItem(content='old')); summary.flush()
    before = summary.current_summary().model_copy(deep=True)
    source = MemoryItem(content='derived', metadata={'source_scopes': [{'item_id': 'original', 'scope': payload}]})
    calls.clear()
    if operation == 'flush':
        summary.add(source)
        with pytest.raises(InvalidCheckpoint): summary.flush()
        assert summary.pending_items() == [source]
    else:
        with pytest.raises(InvalidCheckpoint): summary.reconcile('change', sources=[source])
        assert summary.pending_items() == []
    assert summary.current_summary() == before
    assert calls == []


@pytest.mark.parametrize('layer', ['l1', 'buffer', 'summary'])
@pytest.mark.parametrize('payload', [None, 42, 'invalid-scope', {'raw_history': 'source body'}, {'scope': {'raw_history': 'nested body'}}])
def test_checkpoint_rejects_noncanonical_external_scope_payload_atomically(layer, payload):
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    original, restored = manager(), manager()
    original.write('l1')
    original.summary.add(MemoryItem(content='summarized')); original.summary.flush()
    original.summary.add(MemoryItem(content='pending'))
    cp = original.export_checkpoint().to_dict()
    target = {'l1': cp['state']['l1'][0], 'buffer': cp['state']['l2']['buffer'][0], 'summary': cp['state']['l2']['summary']}[layer]
    target['metadata']['source_scopes'] = [{'item_id': 'original', 'scope': payload}]
    before = restored.export_checkpoint()
    with pytest.raises(InvalidCheckpoint): restored.restore_checkpoint(cp)
    assert restored.export_checkpoint() == before
    original.close(); restored.close()


def test_normalized_nested_scope_payload_remains_compatible_with_summary_and_restore():
    from knowpath_backend.memory.summary import SummaryMemory
    scope = {'item_id': 'original', 'scope': {'scope': {'space_id': 's'}, 'material_ids': ['m']}}
    source = MemoryItem(content='source', metadata={'source_scopes': [scope]})
    summary = SummaryMemory(track_provenance=True)
    summary.add(source); result = summary.flush()
    assert scope in result.metadata['source_scopes']
    original, restored = manager(), manager()
    original.summary.add(source); original.summary.flush()
    cp = original.export_checkpoint()
    restored.restore_checkpoint(cp)
    assert restored.export_checkpoint() == cp
    original.close(); restored.close()
