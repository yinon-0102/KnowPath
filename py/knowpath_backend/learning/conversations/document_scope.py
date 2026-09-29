"""Request-local document selection; never changes persistent publication scopes."""
import hashlib
import json

from ..errors import DomainConflict


def resolve_document_scope(space, material_ids=None):
    bindings = {b['material_id']: b for b in space['bindings']}
    selected = sorted(bindings) if material_ids is None else sorted(set(material_ids))
    if not selected or not set(selected) <= bindings.keys():
        raise DomainConflict('MATERIAL_OUT_OF_SCOPE', '所选资料不在当前学习空间的绑定范围内')
    versions = [{key: bindings[mid][key] for key in
                 ('material_id', 'material_version_id', 'graph_version')} for mid in selected]
    identity = {'space_id': space['id'], 'scope_version': space['scope_version'], 'bindings': versions}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {**identity, 'mode': 'all' if material_ids is None else 'selected',
            'material_ids': selected, 'request_scope_id': fingerprint}


def filter_document_sources(rows, request_scope):
    allowed = {b['material_version_id']: b['material_id'] for b in request_scope['bindings']}
    return [row for row in rows if row.get('material_version_id') in allowed
            and row.get('material_id', allowed.get(row.get('material_version_id'))) == allowed[row['material_version_id']]
            and all(s['material_version_id'] in allowed for s in row.get('source_spans', []))]
