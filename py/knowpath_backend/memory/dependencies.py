"""Bounded canonical dependencies; complete external graphs stay with storage."""
import json

_ID_KEYS = {'id', 'space_id', 'conversation_id', 'message_id', 'item_id', 'material_id',
            'material_version_id', 'version_id', 'source_id', 'source_ref', 'chunk_id',
            'fragment_id', 'section_id', 'dependency_id', 'document_id', 'lesson_id',
            'retrieval_version_id', 'scope_snapshot_id', 'artifact_hash', 'block'}
_VALUE_KEYS = _ID_KEYS | {'kind', 'version', 'material_version', 'source_version', 'scope_version',
                        'context_epoch', 'content_hash', 'source_hash', 'chunk_hash',
                        'fingerprint', 'context_fingerprint', 'lesson_fingerprint',
                        'citation_schema_version', 'start', 'end', 'page'}
_SCOPE_KEYS = {'scope', 'space_id', 'conversation_id', 'context_epoch', 'context_fingerprint',
               'scope_version', 'material_ids', 'material_versions', 'binding', 'answer_mode'}


def canonical_dependency(record):
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint, json_copy
    record = json_copy(record)
    if (type(record) is not dict or not set(record) <= _VALUE_KEYS
            or not any(type(record.get(k)) is str and record[k] for k in _ID_KEYS)
            or any(v is not None and type(v) not in (str, int) for v in record.values())
            or len(json.dumps(record).encode('utf-8')) > 2048):
        raise InvalidCheckpoint('Dependency requires a bounded canonical ID/version record')
    if 'artifact_hash' in record:
        value = record['artifact_hash']
        if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise InvalidCheckpoint('Invalid immutable artifact hash')
    for key in ('retrieval_version_id', 'scope_snapshot_id', 'block'):
        if key in record and record[key] is not None and (type(record[key]) is not str or not 0 < len(record[key]) <= 128):
            raise InvalidCheckpoint('Invalid canonical retrieval/span identifier')
    if 'start' in record or 'end' in record:
        if (not {'material_version_id', 'artifact_hash', 'start', 'end'} <= record.keys()
                or type(record['material_version_id']) is not str or not 0 < len(record['material_version_id']) <= 128
                or type(record['start']) is not int or type(record['end']) is not int
                or record['start'] < 0 or record['end'] <= record['start']):
            raise InvalidCheckpoint('Invalid canonical source span')
    if 'page' in record and record['page'] is not None and (type(record['page']) is not int or record['page'] < 1):
        raise InvalidCheckpoint('Invalid canonical source page')
    if 'citation_schema_version' in record and (type(record['citation_schema_version']) is not int or record['citation_schema_version'] not in (1, 2)):
        raise InvalidCheckpoint('Invalid citation schema version')
    return record


def validate_scope_payload(payload):
    """Canonical scope containers only; binding/material semantics stay external."""
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    if type(payload) is not dict or not set(payload) <= _SCOPE_KEYS:
        raise InvalidCheckpoint('Scope payload requires canonical object fields')
    if 'scope' in payload:
        validate_scope_payload(payload['scope'])


def validate_receipt(value):
    """Validate an opaque server receipt without importing a domain policy."""
    from hashlib import sha256
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    fields = {'space_id', 'conversation_id', 'context_epoch', 'receive_sequence', 'fingerprint', 'identity'}
    if type(value) is not dict or set(value) != fields:
        raise InvalidCheckpoint('Invalid source receipt fields')
    if any(type(value[key]) is not str or not value[key] or len(value[key]) > 512
           for key in ('space_id', 'conversation_id', 'fingerprint')):
        raise InvalidCheckpoint('Invalid source receipt identity')
    if any(type(value[key]) is not int or value[key] < 1
           for key in ('context_epoch', 'receive_sequence')):
        raise InvalidCheckpoint('Invalid source receipt sequence')
    identity = value['identity']
    if type(identity) is not dict or identity.get('space_id') != value['space_id']:
        raise InvalidCheckpoint('Inconsistent source receipt space')
    fingerprint = sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if value['fingerprint'] != fingerprint:
        raise InvalidCheckpoint('Inconsistent source receipt fingerprint')
    if len(json.dumps(value).encode('utf-8')) > 2048:
        raise InvalidCheckpoint('Source receipt record limit exceeded')


def validate_sources(metadata, *, max_dependencies):
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint, json_copy
    result = json_copy(metadata)
    for key in ('source_dependencies', 'source_item_ids', 'source_scopes', 'source_receipts'):
        value = result.get(key, [])
        if type(value) is not list or len(value) > max_dependencies:
            raise InvalidCheckpoint('Derived dependency capacity exceeded')
    for receipt in result.get('source_receipts', []):
        validate_receipt(receipt)
    if 'memory_scope' in result:
        validate_receipt(result['memory_scope'])
    for record in result.get('source_dependencies', []):
        canonical_dependency(record)
    if any(type(value) is not str or not value or len(value) > 512 for value in result.get('source_item_ids', [])):
        raise InvalidCheckpoint('Invalid dependency item ID')
    for record in result.get('source_scopes', []):
        if (type(record) is not dict or set(record) != {'item_id', 'scope'}
                or type(record['item_id']) is not str or not record['item_id']
                or len(json.dumps(record).encode('utf-8')) > 2048):
            raise InvalidCheckpoint('Invalid canonical scope record')
        validate_scope_payload(record['scope'])
    return result


def merge_sources(metadata, sources, *, max_dependencies=512):
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint, json_copy
    result = validate_sources(metadata, max_dependencies=max_dependencies)
    sources = list(sources)
    # Receive order varies within an epoch; it is not scope identity. Keep all
    # contributor receipts while inheriting only a unanimous epoch identity.
    receipts = [source.metadata.get('memory_scope') for source in sources]
    if 'memory_scope' in result:
        receipts.insert(0, result['memory_scope'])
    receipts.extend(result.get('source_receipts', []))
    for source in sources:
        receipts.extend(source.metadata.get('source_receipts', []))
    def receipt_identity(value):
        return {key: item for key, item in value.items() if key != 'receive_sequence'} if isinstance(value, dict) else None
    if receipts and receipts[0] is not None and all(receipt_identity(value) == receipt_identity(receipts[0]) for value in receipts):
        result['memory_scope'] = json_copy(receipts[0])
    elif receipts:
        result.pop('memory_scope', None)
    contributors = result.setdefault('source_receipts', [])
    for source in sources:
        for receipt in [*source.metadata.get('source_receipts', []), source.metadata.get('memory_scope')]:
            if receipt is not None and receipt not in contributors:
                contributors.append(json_copy(receipt))
    if len(contributors) > max_dependencies:
        raise InvalidCheckpoint('Derived receipt capacity exceeded')
    for key in ('source_dependencies', 'source_item_ids', 'source_scopes'):
        unique = []
        for record in result.get(key, []):
            if record not in unique:
                unique.append(record)
        result[key] = unique
    dependencies = result['source_dependencies']
    ids = result['source_item_ids']
    scopes = result['source_scopes']
    for source in sources:
        # A derived source already carries the upstream graph. Validate and copy
        # that graph before combining it with this source's direct identity.
        inherited = validate_sources({key: source.metadata[key]
                                      for key in ('source_dependencies', 'source_item_ids', 'source_scopes')
                                      if key in source.metadata}, max_dependencies=max_dependencies)
        if source.id not in ids:
            ids.append(source.id)
        for item_id in inherited.get('source_item_ids', []):
            if item_id not in ids:
                ids.append(item_id)
        for record in inherited.get('source_dependencies', []):
            if record not in dependencies:
                dependencies.append(record)
        for record in inherited.get('source_scopes', []):
            if record not in scopes:
                scopes.append(record)
        scoped = {key: source.metadata[key] for key in _SCOPE_KEYS if key in source.metadata}
        if scoped:
            scope = {'item_id': source.id, 'scope': json_copy(scoped)}
            if len(json.dumps(scope).encode('utf-8')) > 2048:
                raise InvalidCheckpoint('Scope exceeds canonical record limit')
            if scope not in scopes:
                scopes.append(scope)
        if max(len(ids), len(dependencies), len(scopes)) > max_dependencies:
            raise InvalidCheckpoint('Derived dependency capacity exceeded')
    return validate_sources(result, max_dependencies=max_dependencies)


def source_identity(item):
    """Immutable provenance only; checkpoint heat and pinning may differ."""
    keys = _SCOPE_KEYS | {'memory_scope', 'source_receipts', 'source_dependencies', 'source_item_ids', 'source_scopes', 'source_ref',
                          'source_message_id', 'origin_message_id', 'source_bindings'}
    provenance = {key: item.metadata[key] for key in keys if key in item.metadata}
    from datetime import timezone
    created = item.created_at
    created = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created.astimezone(timezone.utc)
    return (item.content, item.role, item.turn_id or item.metadata.get('turn_id'),
            created, provenance)


def has_provenance(metadata):
    return bool(set(metadata) & (_SCOPE_KEYS | {'memory_scope', 'source_dependencies', 'source_item_ids', 'source_scopes',
                                               'source_ref', 'source_message_id', 'origin_message_id'}))


def canonical_metadata(metadata, *, max_dependencies, max_bytes, source_id=None):
    """Keep bounded provenance, never arbitrary raw history or model payloads."""
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint, json_copy
    keys = _SCOPE_KEYS | {'memory_scope', 'source_receipts', 'source_dependencies', 'source_item_ids', 'source_scopes', 'source_ref',
                          'source_message_id', 'origin_message_id', 'source_bindings'}
    result = json_copy({key: metadata[key] for key in keys if key in metadata})
    if 'scope' in result:
        validate_scope_payload(result['scope'])
    if source_id is not None:
        ids = result.setdefault('source_item_ids', [])
        if source_id not in ids:
            ids.append(source_id)
    for key, value in result.items():
        if key not in {'source_dependencies', 'source_item_ids', 'source_scopes', 'source_receipts'} and len(json.dumps(value).encode('utf-8')) > 2048:
            raise InvalidCheckpoint('Canonical source identity record limit exceeded')
    result = validate_sources(result, max_dependencies=max_dependencies)
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > max_bytes:
        raise InvalidCheckpoint('Canonical source metadata byte limit exceeded')
    return result
