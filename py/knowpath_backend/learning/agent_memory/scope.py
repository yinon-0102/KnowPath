"""Learning-only scope gate. Persistent adapters are raw storage, never prompts.

Task5/7/8/9 must pass every L0-L5, recall, KG and hook candidate through this
gate before scoring. Callbacks are trusted server adapters, never model tools.
No policy callback may perform network IO while a business transaction is held.
"""
from copy import deepcopy

from .repository import digest


def normalize_scope(space, request_scope, *, entry_type='chat', reading_context=None,
                    lesson_fingerprint=None, answer_mode='material'):
    """Canonical request identity; auto-all and explicitly selected-all differ."""
    if entry_type not in ('chat', 'reading', 'lesson') or answer_mode not in ('material', 'general', 'auto'):
        raise ValueError('Unsupported memory entry or answer mode')
    if request_scope.get('space_id') != space['id'] or request_scope.get('scope_version') != space['scope_version']:
        raise ValueError('Request scope is stale or belongs to another space')
    if request_scope.get('mode') not in ('all', 'selected'): raise ValueError('Selection mode is required')
    def canonical_bindings(values):
        result = {}
        for value in values:
            if any(type(value.get(k)) is not str or not value[k] for k in ('material_id', 'material_version_id')):
                raise ValueError('Incomplete material binding')
            key = value['material_id']
            if key in result and result[key] != value: raise ValueError('Conflicting material binding')
            result[key] = deepcopy(value)
        return [result[k] for k in sorted(result)]
    bindings = canonical_bindings(space['bindings'])
    selected = sorted(set(request_scope['material_ids']))
    expected = [b for b in bindings if b['material_id'] in selected]
    if len(expected) != len(selected) or canonical_bindings(request_scope['bindings']) != expected:
        raise ValueError('Request materials do not match current bindings')
    if request_scope['mode'] == 'all' and selected != [b['material_id'] for b in bindings]:
        raise ValueError('Automatic all must include every binding')
    if entry_type == 'reading':
        if not isinstance(reading_context, dict) or not any(
            b['material_id'] == reading_context.get('material_id') and
            b['material_version_id'] == reading_context.get('material_version_id') for b in expected):
            raise ValueError('Reading requires the current original version')
    elif reading_context is not None: raise ValueError('Reading context requires reading entry')
    if entry_type == 'lesson':
        if type(lesson_fingerprint) is not str or not lesson_fingerprint: raise ValueError('Lesson fingerprint required')
    elif lesson_fingerprint is not None: raise ValueError('Lesson fingerprint requires lesson entry')
    return dict(space_id=space['id'], scope_version=space['scope_version'], bindings=bindings,
                mode=request_scope['mode'], material_ids=selected, entry_type=entry_type,
                reading_context=deepcopy(reading_context), lesson_fingerprint=lesson_fingerprint, answer_mode=answer_mode)


def scope_fingerprint(identity): return digest(identity)


def freeze_reception(repository, message_id, identity):
    """Freeze reception in the SAME outer transaction as message acceptance.

    Lock order: caller business-space lock, then memory-space lock. No completed
    status filter: a failed/cancelled accepted request remains an epoch boundary.
    This allocator does not authorize multiple in-flight HTTP requests.
    """
    state = repository._locked_space()
    message = repository._business_message(message_id)
    if not message or message['space_id'] != repository.space_id or identity['space_id'] != repository.space_id:
        raise ValueError('Reception message belongs to another space')
    fingerprint = scope_fingerprint(identity)
    old = message['snapshot'].get('memory_scope')
    if old is not None:
        if old['fingerprint'] != fingerprint: raise ValueError('Idempotent reception identity conflict')
        return deepcopy(old)
    if message['status'] != 'pending': raise ValueError('Only newly accepted pending messages may allocate')
    previous = [row['snapshot']['memory_scope'] for row in repository._business_messages(message['conversation_id'])
                if row['id'] != message_id and 'memory_scope' in row.get('snapshot', {})]
    latest = max(previous, key=lambda value: value['receive_sequence'], default=None)
    epoch = 1 if latest is None else latest['context_epoch'] + (latest['fingerprint'] != fingerprint)
    frozen = dict(space_id=repository.space_id, conversation_id=message['conversation_id'],
        context_epoch=epoch, receive_sequence=state['receive_sequence'] + 1,
        fingerprint=fingerprint, identity=deepcopy(identity))
    snapshot = deepcopy(message['snapshot'])
    snapshot['memory_scope'] = frozen
    repository._save_business_snapshot(message_id, snapshot)
    repository._put('spaces', dict(state, receive_sequence=frozen['receive_sequence']))
    return deepcopy(frozen)


def _metadata(value):
    return value.get('metadata', {}) if isinstance(value, dict) else value.metadata


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def complete_dependencies(value, dependency_sets=None):
    metadata = _metadata(value)
    dependencies = metadata.get('source_dependencies')
    ref = metadata.get('source_ref') or _field(value, 'source_ref')
    if ref and ref.startswith('dependencies:'):
        if dependency_sets is None: raise ValueError('Full dependency set is unavailable')
        full = dependency_sets(ref.split(':', 1)[1])
        if not full or any(dep not in full for dep in dependencies or []):
            raise ValueError('Incomplete or inconsistent external dependency set')
        dependencies = full
    if type(dependencies) is not list or any(type(dep) is not dict or not dep for dep in dependencies):
        raise ValueError('Dependencies are unknown')
    return deepcopy(dependencies)


class EligibilityPolicy:
    """One deny-by-default gate shared by every memory consumer.

    preference_verifier must check a server-owned attestation bound to immutable
    candidate identity/content/provenance. Model metadata and user_explicit source
    are never attestations. Default is no exemptions. Tombstones take priority.
    dependency_resolver must validate existence, ownership, version/hash and any
    transitive source references; an unknown result is a denial.
    """
    PURPOSES = frozenset(('L0', 'L1', 'L2', 'L3', 'L4', 'L5', 'recall', 'hook', 'kg'))

    def __init__(self, current, *, dependency_resolver, tombstone_checker,
                 dependency_sets=None, preference_verifier=None):
        self.current = deepcopy(current)
        self.dependency_resolver = dependency_resolver
        self.tombstone_checker = tombstone_checker
        self.dependency_sets = dependency_sets
        self.preference_verifier = preference_verifier or (lambda value: False)

    def eligible(self, value, *, purpose):
        if purpose not in self.PURPOSES: raise ValueError('Unknown memory read purpose')
        try:
            metadata = _metadata(value)
            origin = metadata.get('memory_scope')
            if not origin or origin['space_id'] != self.current['space_id']: return False
            if origin['fingerprint'] != scope_fingerprint(origin['identity']): return False
            if self.current['fingerprint'] != scope_fingerprint(self.current['identity']): return False
            if metadata.get('quarantined') or _field(value, 'lifecycle', 'active') != 'active': return False
            targets = [('item', _field(value, 'id')), ('message', metadata.get('source_message_id')),
                       ('source', metadata.get('source_version'))]
            if any(target and self.tombstone_checker(kind, target) is not False for kind, target in targets): return False
            dependencies = complete_dependencies(value, self.dependency_sets)
            preference = (metadata.get('memory_kind') == 'preference' and metadata.get('source_independent') is True
                          and not dependencies and self.preference_verifier(value) is True)
            if not preference and origin['fingerprint'] != self.current['fingerprint']: return False
            if purpose in ('L1', 'L2', 'L3') and any(origin[key] != self.current[key] for key in ('conversation_id', 'context_epoch')):
                return False
            if not preference and not dependencies: return False
            selected = {b['material_id']: b['material_version_id'] for b in self.current['identity']['bindings']
                        if b['material_id'] in self.current['identity']['material_ids']}
            for dep in dependencies:
                if self.tombstone_checker('dependency', dep) is not False: return False
                if 'material_id' in dep or 'material_version_id' in dep:
                    if selected.get(dep.get('material_id')) != dep.get('material_version_id') or not dep.get('material_version_id'):
                        return False
                if self.dependency_resolver(dep) is not True: return False
            return True
        except Exception:
            # Read failure cannot become an empty tombstone/dependency set.
            return False

    def filter(self, values, *, purpose):
        return [deepcopy(value) for value in values if self.eligible(value, purpose=purpose)]

    def rank(self, values, *, purpose, key, reverse=True):
        return sorted(self.filter(values, purpose=purpose), key=key, reverse=reverse)


def inherit_dependencies(parents, *, dependency_sets=None):
    """Derivation cannot erase source taint; independent proof is minted elsewhere."""
    records = []
    for value in parents:
        for dep in complete_dependencies(value, dependency_sets):
            if dep not in records: records.append(dep)
    return dict(memory_kind='source', source_independent=False, source_dependencies=records)
