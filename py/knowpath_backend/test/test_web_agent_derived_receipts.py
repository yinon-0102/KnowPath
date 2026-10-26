"""Derived provenance must survive normal use and reject damaged hot state."""
from copy import deepcopy

import pytest

from knowpath_backend.learning.agent_memory.scope import scope_fingerprint
from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
from knowpath_backend.memory import MemoryConfig, MemoryManager
from knowpath_backend.memory.checkpoint import InvalidCheckpoint
from knowpath_backend.memory.dependencies import validate_sources
from knowpath_backend.memory.item import MemoryItem


def receipt(sequence):
    identity = dict(
        space_id='space-123', scope_version=1,
        bindings=[dict(material_id='material-123', material_version_id='version-123')],
        mode='all', material_ids=['material-123'], entry_type='chat',
        reading_context=None, lesson_fingerprint=None, answer_mode='material',
    )
    return dict(
        space_id=identity['space_id'], conversation_id='conversation-123',
        context_epoch=1, receive_sequence=sequence,
        fingerprint=scope_fingerprint(identity), identity=identity,
    )


def make_manager(**options):
    return MemoryManager(
        MemoryConfig(tick_mode='off', conflict_strength='off', **options),
        context_policy=WebContextPolicy(),
    )


def test_multiturn_summary_can_be_remembered_without_losing_contributors():
    manager, restored = make_manager(), make_manager()
    try:
        sources = []
        for sequence in range(1, 9):
            source = MemoryItem(
                content=f'Explanation from turn {sequence}', role='user',
                turn_id=f'message-{sequence}',
                metadata=dict(
                    memory_scope=receipt(sequence),
                    source_dependencies=[dict(message_id=f'message-{sequence}')],
                ),
            )
            sources.append(source)
            manager.summary.add(source)
            manager.summary.flush()
        summary = manager.summary.current_summary()
        assert summary.metadata['source_receipts'] == [receipt(n) for n in range(1, 9)]
        assert set(summary.metadata['source_item_ids']) == {source.id for source in sources}
        assert summary.metadata['source_dependencies'] == [
            dict(message_id=f'message-{n}') for n in range(1, 9)
        ]
        card = manager.remember('The complete conversation summary', metadata=summary.metadata)
        assert card.metadata['source_receipts'] == summary.metadata['source_receipts']
        assert card.metadata['source_dependencies'] == summary.metadata['source_dependencies']
        assert card.metadata['source_item_ids'] == summary.metadata['source_item_ids']
        restored.restore_checkpoint(manager.export_checkpoint())
        restored_summary = restored.summary.current_summary()
        restored_card = restored.remember('Summary after restart', metadata=restored_summary.metadata)
        assert restored_card.metadata == card.metadata
    finally:
        manager.close()
        restored.close()


def damaged_receipts(case):
    if case == 'container':
        return 'not-a-list'
    if case == 'element':
        return [None]
    if case == 'missing-field':
        value = receipt(1)
        del value['identity']
        return [value]
    if case == 'capacity':
        return [receipt(n) for n in range(1, 4)]
    raise AssertionError(case)


@pytest.mark.parametrize('case', ['container', 'element', 'missing-field', 'capacity'])
def test_validate_sources_rejects_damaged_receipts(case):
    with pytest.raises(InvalidCheckpoint):
        validate_sources({'source_receipts': damaged_receipts(case)}, max_dependencies=2)


@pytest.mark.parametrize('layer', ['l1', 'summary', 'buffer'])
@pytest.mark.parametrize('case', ['container', 'element', 'missing-field', 'capacity'])
def test_damaged_receipts_checkpoint_restore_is_atomic(layer, case):
    original = make_manager(summary_max_dependencies=2)
    restored = make_manager(summary_max_dependencies=2)
    try:
        source = MemoryItem(content='checkpoint source', metadata={
            'memory_scope': receipt(1), 'source_receipts': [receipt(1)],
            'source_dependencies': [{'message_id': 'message-1'}],
        })
        if layer == 'l1':
            original.write(source.content, turn_id='message-1', metadata=source.metadata)
        else:
            original.summary.add(source)
            if layer == 'summary':
                original.summary.flush()
        checkpoint = deepcopy(original.export_checkpoint().to_dict())
        if layer == 'l1':
            target = checkpoint['state']['l1'][0]
        elif layer == 'summary':
            target = checkpoint['state']['l2']['summary']
        else:
            target = checkpoint['state']['l2']['buffer'][0]
        target['metadata']['source_receipts'] = damaged_receipts(case)
        restored.write('Existing state must survive', turn_id='existing-turn')
        before = restored.export_checkpoint()
        with pytest.raises(InvalidCheckpoint):
            restored.restore_checkpoint(checkpoint)
        assert restored.export_checkpoint() == before
    finally:
        original.close()
        restored.close()
