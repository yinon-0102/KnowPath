"""Versioned, bounded session hot state. Backends and model clients stay external."""
import json
from pydantic import ValidationError
from knowpath_backend.memory.contracts import MemoryCheckpoint
from knowpath_backend.memory.item import MemoryItem
from knowpath_backend.memory.recall_buffer import RecallEntry


class InvalidCheckpoint(ValueError):
    """Unsupported, malformed or over-capacity hot state; nothing is restored."""


class CheckpointBusy(RuntimeError):
    """An async tick owns the mutable state. Retry after maintenance completes."""


def json_copy(value):
    """Validate finite JSON values and detach all mutable containers."""
    def check(v):
        if type(v) is dict:
            if any(type(k) is not str for k in v):
                raise InvalidCheckpoint('Checkpoint keys must be strings')
            for child in v.values(): check(child)
        elif type(v) is list:
            for child in v: check(child)
        elif v is not None and type(v) not in (str, bool, int, float):
            raise InvalidCheckpoint('Checkpoint requires JSON values')
    try:
        check(value)
        return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidCheckpoint('Checkpoint requires finite, serializable JSON values') from exc


def dump_item(item):
    result = item.model_dump()
    for key in ('created_at', 'last_access'):
        result[key] = result[key].isoformat()
    return json_copy(result)


def export_state(manager):
    return json_copy({
        'l1': [dump_item(it) for it in manager.working.items()],
        'l2': {'summary': dump_item(manager.summary.current_summary()) if manager.summary.current_summary() else None,
               'buffer': [dump_item(it) for it in manager.summary.pending_items()]},
        'l3': [entry.model_dump() for entry in manager.recall_buffer.entries()],
        'counters': {'turn': manager._turn_counter, 'reflect': manager._last_reflect_turn,
                     'reconcile': manager._last_reconcile_turn},
        'whole_turns': manager.working.whole_turns,
    })


def _item(value):
    if type(value) is not dict or set(value) != set(MemoryItem.model_fields):
        raise InvalidCheckpoint('Invalid checkpoint item fields')
    item = MemoryItem.model_validate_json(json.dumps(value), strict=True)
    if not item.id or item.access_count < 0 or item.token_estimate < 1 or not 0 <= item.prior_score <= 1:
        raise InvalidCheckpoint('Invalid checkpoint item state')
    return item


def parse_checkpoint(checkpoint, config):
    """Fully validate before callers replace any live state."""
    try:
        # Avoid dataclass.asdict() deep-copying arbitrary non-JSON objects first.
        payload = vars(checkpoint) if isinstance(checkpoint, MemoryCheckpoint) else checkpoint
        payload = json_copy(payload)
        if type(payload) is not dict or set(payload) != set(MemoryCheckpoint.__dataclass_fields__) - {'VERSION'}:
            raise InvalidCheckpoint('Invalid checkpoint envelope')
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > config.checkpoint_max_bytes:
            raise InvalidCheckpoint('Checkpoint byte limit exceeded')
        for key in ('schema_version', 'revision', 'context_epoch'):
            if type(payload[key]) is not int or payload[key] < 0:
                raise InvalidCheckpoint('Invalid checkpoint envelope counter')
        for key in ('space_id', 'conversation_id', 'context_fingerprint'):
            if type(payload[key]) is not str:
                raise InvalidCheckpoint('Invalid checkpoint identity')
        cp = MemoryCheckpoint.from_dict(payload)
        state = cp.state
        if type(state) is not dict or set(state) != {'l1', 'l2', 'l3', 'counters', 'whole_turns'}:
            raise InvalidCheckpoint('Invalid checkpoint state fields')
        if type(state['whole_turns']) is not bool:
            raise InvalidCheckpoint('Invalid whole_turns flag')
        counters = state['counters']
        if type(counters) is not dict or set(counters) != {'turn', 'reflect', 'reconcile'} or any(type(v) is not int or v < 0 for v in counters.values()):
            raise InvalidCheckpoint('Invalid checkpoint counters')
        if counters['reflect'] > counters['turn'] or counters['reconcile'] > counters['turn']:
            raise InvalidCheckpoint('Invalid checkpoint counter order')
        if type(state['l1']) is not list or type(state['l3']) is not list:
            raise InvalidCheckpoint('Invalid checkpoint lists')
        l1 = [_item(it) for it in state['l1']]
        l2 = state['l2']
        if type(l2) is not dict or set(l2) != {'summary', 'buffer'} or type(l2['buffer']) is not list:
            raise InvalidCheckpoint('Invalid L2 checkpoint')
        if len(l2['buffer']) > config.checkpoint_max_buffer_entries:
            raise InvalidCheckpoint('Checkpoint pending buffer limit exceeded')
        summary = _item(l2['summary']) if l2['summary'] is not None else None
        buffer = [_item(it) for it in l2['buffer']]
        from knowpath_backend.memory.dependencies import validate_sources
        for item in [*l1, *buffer, *([summary] if summary is not None else [])]:
            # Canonical external provenance must be valid in every hot layer.
            # Other raw item metadata keeps its generic legacy representation.
            provenance = {key: item.metadata[key] for key in
                          ('source_dependencies', 'source_item_ids', 'source_scopes') if key in item.metadata}
            validate_sources(provenance, max_dependencies=config.summary_max_dependencies)
        if len(state['l3']) > config.l3_max_entries:
            raise InvalidCheckpoint('Checkpoint recall capacity exceeded')
        entries = []
        for value in state['l3']:
            if type(value) is not dict or set(value) != set(RecallEntry.model_fields):
                raise InvalidCheckpoint('Invalid recall fields')
            entry = RecallEntry.model_validate(value, strict=True)
            if entry.hit_count < 1 or not entry.item_id or not 0 <= entry.hit_score <= 1 or not 0 <= entry.first_recalled_turn <= entry.last_recalled_turn <= counters['turn']:
                raise InvalidCheckpoint('Invalid recall entry state')
            entries.append(entry)
        if len({it.id for it in l1}) != len(l1) or len({e.item_id for e in entries}) != len(entries):
            raise InvalidCheckpoint('Duplicate checkpoint entries')
        return cp, l1, summary, buffer, entries
    except InvalidCheckpoint:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError, RecursionError) as exc:
        raise InvalidCheckpoint('Invalid or unsupported memory checkpoint') from exc
