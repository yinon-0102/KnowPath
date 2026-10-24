"""SQL and memory adapters share one validated persistence algebra.

Writes always join the business transaction. Reads detach values and hide corrupt
or invalid projections; only explicit repair may change stored projections.
"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from functools import wraps
from hashlib import sha256
import json
import math
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from knowpath_backend.memory.checkpoint import json_copy
from knowpath_backend.memory.contracts import MemoryCheckpoint, MemoryMutation
from knowpath_backend.memory.dependencies import canonical_dependency
from knowpath_backend.memory.item import MemoryItem
from knowpath_backend.memory.playbook.card import PlaybookCard
from knowpath_backend.learning.agent_memory.models import TABLES
from knowpath_backend.learning.persistence.db import LearningMessageRow, ConversationRow


class InvalidMutation(ValueError): pass


def consistent_read(method):
    """Hold one memory snapshot across every step of a detached public read."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        repository = getattr(self, 'repository', self)
        with repository._read_guard():
            return method(self, *args, **kwargs)
    return guarded


def digest(value):
    return sha256(json.dumps(json_copy(value), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def item_id_for(message_id, role):
    if role not in ('user', 'assistant'): raise ValueError('Unsupported message role')
    return str(uuid5(NAMESPACE_URL, f'knowpath:message:{message_id}:{role}'))


def stable_id(*parts): return str(uuid5(NAMESPACE_URL, json.dumps(parts)))


def projection_provenance(message, *, dependency_limit=None):
    """Extract frozen identities, never prompt/history/source prose.

    Scope eligibility against today's bindings belongs to the scope policy.
    Missing historical identity is retained as quarantined L4, not guessed.
    """
    snapshot, response = message.get('snapshot'), message.get('response')
    if type(snapshot) is not dict or type(response) is not dict: raise ValueError('Source snapshot and response require JSON objects')
    scope = snapshot.get('request_scope')
    known = (type(scope) is dict and scope.get('space_id') == message['space_id']
        and type(scope.get('scope_version')) is int and scope['scope_version'] >= 0
        and type(scope.get('request_scope_id')) is str and bool(scope['request_scope_id'])
        and type(scope.get('bindings')) is list and type(scope.get('material_ids')) is list)
    bindings, dependencies = [], []
    if known:
        for binding in scope['bindings']:
            if type(binding) is not dict or any(type(binding.get(key)) is not str or not binding[key] for key in ('material_id', 'material_version_id')):
                known = False
                break
            record = {key: binding[key] for key in ('material_id', 'material_version_id')}
            if type(binding.get('graph_version')) is int: record['source_version'] = binding['graph_version']
            bindings.append(canonical_dependency(record))
        if any(type(mid) is not str for mid in scope['material_ids']) or set(scope['material_ids']) != {b['material_id'] for b in bindings}: known = False
        if response.get('request_scope', scope) != scope: known = False
    identity_keys = ('id', 'space_id', 'message_id', 'item_id', 'material_id', 'material_version_id', 'version_id',
        'source_id', 'source_ref', 'chunk_id', 'fragment_id', 'section_id', 'document_id', 'lesson_id',
        'kind', 'version', 'source_version', 'content_hash', 'source_hash', 'chunk_hash', 'fingerprint',
        'retrieval_version_id', 'scope_snapshot_id', 'artifact_hash', 'citation_schema_version', 'start', 'end', 'page', 'block')
    def add_refs(values):
        if type(values) is not list: raise ValueError('Invalid canonical source list')
        for value in values:
            if type(value) is not dict: raise ValueError('Invalid canonical source identity')
            record = {k: value[k] for k in identity_keys if k in value}
            spans = value.get('source_spans', [])
            if type(spans) is not list: raise ValueError('Invalid source span container')
            canonical_spans = []
            for span in spans:
                if type(span) is not dict: raise ValueError('Invalid source span identity')
                if not {'material_version_id', 'artifact_hash', 'start', 'end'} <= span.keys(): raise ValueError('Incomplete source span identity')
                version_id = span['material_version_id']
                if type(version_id) is not str or not 0 < len(version_id) <= 128 or version_id != record.get('material_version_id'):
                    raise ValueError('Source span requires a valid version matching its citation')
                canonical_spans.append(canonical_dependency({k: span[k] for k in identity_keys if k in span}))
            if 'source_spans' in value:
                # Preserve each citation's ordered group even when two parents
                # have identical headers or share the same original spans.
                record['source_ref'] = 'citation:' + digest({'record': record, 'spans': canonical_spans})
            record = canonical_dependency(record)
            if record not in dependencies: dependencies.append(record)
            parent = {k: record[k] for k in ('material_id', 'material_version_id', 'retrieval_version_id', 'chunk_id', 'citation_schema_version', 'source_ref') if k in record}
            for span in canonical_spans:
                linked = canonical_dependency({**parent, **span})
                if linked not in dependencies: dependencies.append(linked)
    add_refs(bindings)
    add_refs(snapshot.get('delivered_source_refs', []))
    add_refs(response.get('citations', []))
    add_refs(response.get('source_dependencies', []))
    metadata = {'source_message_id': message['id'], 'source_dependencies': dependencies}
    if known:
        metadata['scope'] = {'space_id': message['space_id'], 'scope_version': scope['scope_version'],
            'material_ids': list(scope['material_ids']), 'material_versions': bindings,
            'context_fingerprint': scope['request_scope_id']}
        mode = response.get('answer_mode', snapshot.get('requested_answer_mode'))
        if type(mode) is str: metadata['scope']['answer_mode'] = mode
        metadata['source_bindings'] = {key: value for key, value in {
            'selection_mode': scope.get('mode'), 'requested_answer_mode': snapshot.get('requested_answer_mode')}.items() if type(value) is str}
        for key in ('lesson_id', 'lesson_context_fingerprint'):
            if type(snapshot.get(key)) is str: metadata.setdefault('source_bindings', {})[key] = snapshot[key]
        reading = snapshot.get('reading_context')
        if type(reading) is dict:
            metadata.setdefault('source_bindings', {})['reading_context'] = {k: reading[k] for k in identity_keys if k in reading and type(reading[k]) in (str, int)}
    if dependencies: metadata['source_ref'] = 'dependencies:' + stable_id(message['id'], digest(dependencies))
    version = digest(metadata)
    if dependency_limit is not None: metadata['source_dependencies'] = dependencies[:dependency_limit]
    return version, json_copy(metadata), known


class MemoryRepository:
    def _read_guard(self): return nullcontext()
    def _write(self):
        if not self.active: raise RuntimeError('Memory persistence requires an active outer transaction')

    def ensure_space(self):
        self._write()
        if self._one('spaces', space_id=self.space_id) is None:
            if not self._business_space(): raise ValueError('Unknown learning space')
            self._put('spaces', dict(space_id=self.space_id, revision=0, deletion_generation=0,
                receive_sequence=0, migration_status='pending', lease_token=None, lease_owner=None, lease_expires=None))
        return self.get_space()

    @consistent_read
    def get_space(self): return self._one('spaces', space_id=self.space_id)

    def _locked_space(self):
        self._write()
        value = self._lock_space()
        if value is None: raise ValueError('Memory space has not been initialized')
        return value

    def _tombstoned(self, kind, target):
        return bool(self._rows('tombstones', kind=kind, target_hash=digest(target)))

    def _message(self, mid):
        row = self._business_message(mid)
        if not row or row['space_id'] != self.space_id or row['status'] != 'completed':
            raise ValueError('Projection requires a completed message in this space')
        response = row.get('response')
        if type(row.get('message')) is not str or not row['message'].strip() or type(response) is not dict or type(response.get('text')) is not str or not response['text'].strip():
            raise ValueError('Completed message requires legal message and response.text bodies')
        return row

    def _source(self, row):
        return projection_provenance(row, dependency_limit=512)

    def project_completed(self, message_id):
        self._locked_space()
        message = self._message(message_id)
        version, metadata, known = self._source(message)
        if self._tombstoned('source', version): return []
        full_dependencies = projection_provenance(message)[1]['source_dependencies']
        if full_dependencies:
            self.save_dependency_set(metadata['source_ref'].split(':', 1)[1], message_id,
                full_dependencies, owner_message_id=message_id)
        result = []
        for role, content in (('user', message['message']), ('assistant', message['response']['text'])):
            iid = item_id_for(message_id, role)
            if self._tombstoned('item', iid): continue
            old = self._one('items', item_id=iid)
            if old and self._valid_item(old) is not None:
                result.append(iid)
                continue
            if old:
                # Explicit quarantine and vector invalidation precede body repair.
                self._put('items', dict(old, lifecycle='quarantined', vector=None, index_generation=old['index_generation'] + 1))
            created = message.get('created_at')
            if isinstance(created, str):
                from datetime import datetime
                created = datetime.fromisoformat(created)
            kwargs = {'created_at': created, 'last_access': created} if created else {}
            item = MemoryItem(id=iid, turn_id=message_id, role=role, content=content,
                metadata=metadata, **kwargs)
            self._put('items', dict(item_id=iid, space_id=self.space_id, message_id=message_id, role=role,
                content=content, checksum=digest(content), lifecycle='active' if known else 'quarantined', access_count=0,
                scope_hash=digest(metadata.get('scope')), source_version=version,
                payload=item.model_dump(mode='json'), vector=None, embedding_model=None, embedding_dimension=None,
                embedding_type='document', index_generation=(old['index_generation'] + 1 if old else 0)))
            result.append(iid)
        return result

    def _valid_item(self, row):
        if not row or row['lifecycle'] != 'active' or self._tombstoned('item', row['item_id']) or self._tombstoned('source', row['source_version']): return None
        try:
            message = self._message(row['message_id'])
            version, metadata, known = self._source(message)
            text = message['message'] if row['role'] == 'user' else message['response']['text']
            item = MemoryItem.model_validate(deepcopy(row['payload']))
            if (row['role'] not in ('user', 'assistant') or row['item_id'] != item_id_for(message['id'], row['role'])
                    or version != row['source_version'] or row['content'] != text or row['checksum'] != digest(text)
                    or item.content != text or item.id != row['item_id'] or item.turn_id != message['id']
                    or item.role != row['role'] or item.metadata != metadata or not known): return None
            return item
        except (ValueError, TypeError, KeyError): return None

    @consistent_read
    def get_item(self, item_id): return self._valid_item(self._one('items', item_id=item_id))
    @consistent_read
    def items_for_turn(self, turn_id): return [item for row in self._rows('items', message_id=turn_id) if (item := self._valid_item(row))]
    @consistent_read
    def list_items(self): return [item for row in self._rows('items') if (item := self._valid_item(row))]

    def update_item(self, item):
        self._locked_space()
        item = MemoryItem.model_validate(json_copy(item.model_dump(mode='json') if isinstance(item, MemoryItem) else item))
        old = self.get_item(item.id)
        from knowpath_backend.memory.dependencies import source_identity
        if old is None or source_identity(old) != source_identity(item): raise ValueError('Item content/provenance is authoritative')
        if item.access_count < old.access_count or not math.isfinite(item.prior_score) or not 0 <= item.prior_score <= 1: raise ValueError('Invalid item state')
        if any(type(iid) is not str or self.get_item(iid) is None for iid in item.supersedes): raise ValueError('Invalid supersede reference')
        if item.superseded_by is not None and self.get_item(item.superseded_by) is None: raise ValueError('Invalid replacement reference')
        row = self._one('items', item_id=item.id)
        self._put('items', dict(row, payload=item.model_dump(mode='json'), access_count=item.access_count))

    def repair_item(self, item_id):
        self._locked_space()
        row = self._one('items', item_id=item_id)
        if not row or self._tombstoned('item', item_id) or self._tombstoned('source', row['source_version']): return None
        try: self.project_completed(row['message_id'])
        except ValueError:
            self._put('items', dict(row, lifecycle='quarantined', vector=None, index_generation=row['index_generation'] + 1))
            return None
        return self.get_item(item_id)

    def _tombstone(self, kind, target, generation):
        key = digest(target)
        self._put('tombstones', dict(tombstone_id=stable_id(self.space_id, kind, key), space_id=self.space_id,
            target_hash=key, kind=kind, deletion_generation=generation))

    def forget_item(self, item_id):
        state = self._locked_space()
        state['deletion_generation'] += 1
        self._put('spaces', state)
        self._tombstone('item', item_id, state['deletion_generation'])
        self._delete('items', item_id=item_id)

    def invalidate_source(self, source_version):
        state = self._locked_space()
        state['deletion_generation'] += 1
        self._put('spaces', state)
        self._tombstone('source', source_version, state['deletion_generation'])
        for row in self._rows('items', source_version=source_version):
            self._put('items', dict(row, lifecycle='invalid', vector=None, index_generation=row['index_generation'] + 1))

    def put_card(self, card):
        self._locked_space()
        card = PlaybookCard.model_validate(json_copy(card.model_dump(mode='json') if isinstance(card, PlaybookCard) else card))
        if not card.id or len(card.id) > 36 or not math.isfinite(card.confidence) or not 0 <= card.confidence <= 1: raise ValueError('Invalid card')
        source_hash = digest([card.source.value, card.source_ref or card.id])
        duplicate = self._rows('cards', source_hash=source_hash)
        if duplicate and duplicate[0]['card_id'] != card.id: raise ValueError('Duplicate card source')
        self._put('cards', dict(card_id=card.id, space_id=self.space_id, source_hash=source_hash,
            scope_hash=digest(card.metadata.get('scope', {})), lifecycle=card.lifecycle.value,
            pinned=card.user_pinned, payload=card.model_dump(mode='json')))

    @consistent_read
    def get_card(self, card_id):
        row = self._one('cards', card_id=card_id)
        return PlaybookCard.model_validate(deepcopy(row['payload'])) if row else None

    @consistent_read
    def list_cards(self): return [PlaybookCard.model_validate(row['payload']) for row in self._rows('cards')]
    def delete_card(self, card_id): self._locked_space(); self._delete('cards', card_id=card_id)

    def save_dependency_set(self, set_id, derived_id, records, owner_message_id=None):
        self._locked_space()
        if type(set_id) is not str or not 0 < len(set_id) <= 128 or type(derived_id) is not str or not 0 < len(derived_id) <= 128 or type(records) is not list or not records:
            raise ValueError('Dependency sets require identity and complete nonempty records')
        records = [canonical_dependency(record) for record in records]
        if len({digest(r) for r in records}) != len(records): raise ValueError('Duplicate dependency records')
        if owner_message_id is not None:
            self._message(owner_message_id)
        existing = self.get_dependency_set(set_id)
        if existing is not None:
            if existing != records or self._rows('dependencies', set_id=set_id)[0]['derived_id'] != derived_id: raise ValueError('Dependency set is immutable')
            return
        for ordinal, record in enumerate(records):
            self._put('dependencies', dict(dependency_id=stable_id(self.space_id, set_id, ordinal), space_id=self.space_id,
                set_id=set_id, derived_id=derived_id, ordinal=ordinal, record_hash=digest(record), record=record,
                owner_message_id=owner_message_id,
                **{key: record.get(key) for key in ('message_id', 'item_id', 'material_version_id', 'chunk_id', 'retrieval_version_id', 'artifact_hash', 'block')}))

    @consistent_read
    def get_dependency_set(self, set_id):
        rows = self._rows('dependencies', set_id=set_id)
        return [r['record'] for r in sorted(rows, key=lambda r: r['ordinal'])] if rows else None

    @consistent_read
    def dependents(self, dependency):
        dependency = canonical_dependency(dependency)
        filters = {k: v for k, v in dependency.items() if k in ('message_id', 'item_id', 'material_version_id', 'chunk_id', 'retrieval_version_id', 'artifact_hash', 'block')}
        return sorted({row['derived_id'] for row in self._rows('dependencies', **filters)
                       if all(row['record'].get(k) == v for k, v in dependency.items())})

    def save_checkpoint(self, checkpoint):
        self._locked_space()
        cp = MemoryCheckpoint.from_dict(json_copy(checkpoint.to_dict() if isinstance(checkpoint, MemoryCheckpoint) else checkpoint))
        if cp.space_id != self.space_id or not self._business_conversation(cp.conversation_id): raise ValueError('Checkpoint conversation belongs to another space')
        if type(cp.context_epoch) is not int or cp.context_epoch < 0 or type(cp.revision) is not int or cp.revision < 0 or type(cp.state) is not dict: raise ValueError('Invalid checkpoint')
        refs = cp.state.get('dependency_set_ids', [])
        if type(refs) is not list or any(type(ref) is not str or self.get_dependency_set(ref) is None for ref in refs): raise ValueError('Dangling dependency set reference')
        self._put('sessions', dict(session_id=stable_id(self.space_id, cp.conversation_id, cp.context_epoch), space_id=self.space_id,
            conversation_id=cp.conversation_id, context_epoch=cp.context_epoch, fingerprint=cp.context_fingerprint,
            revision=cp.revision, version=1, checkpoint=cp.to_dict()))

    @consistent_read
    def get_checkpoint(self, conversation_id, context_epoch):
        row = self._one('sessions', conversation_id=conversation_id, context_epoch=context_epoch)
        return MemoryCheckpoint.from_dict(row['checkpoint']) if row else None

    def index_identity(self, item_id, *, model, dimension):
        state = self._locked_space()
        if not self.get_item(item_id): raise ValueError('Cannot index unavailable item')
        if type(model) is not str or not 0 < len(model) <= 128 or type(dimension) is not int or dimension < 1: raise ValueError('Invalid embedding target')
        row = self._one('items', item_id=item_id)
        if row['embedding_model'] != model or row['embedding_dimension'] != dimension:
            row.update(embedding_model=model, embedding_dimension=dimension, vector=None, index_generation=row['index_generation'] + 1)
            self._put('items', row)
        return dict(item_id=item_id, checksum=row['checksum'], source_version=row['source_version'],
            deletion_generation=state['deletion_generation'], index_generation=row['index_generation'],
            model=model, dimension=dimension, embedding_type='document')

    def complete_index(self, identity, vector):
        state = self._locked_space()
        identity, vector = json_copy(identity), json_copy(vector)
        if type(vector) is not list or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector): raise ValueError('Invalid embedding vector')
        row = self._one('items', item_id=identity.get('item_id'))
        if not row or not self._valid_item(row): return False
        expected = dict(item_id=row['item_id'], checksum=row['checksum'], source_version=row['source_version'],
            deletion_generation=state['deletion_generation'], index_generation=row['index_generation'],
            model=row['embedding_model'], dimension=row['embedding_dimension'], embedding_type='document')
        if identity != expected: return False
        if len(vector) != row['embedding_dimension']: raise ValueError('Embedding dimension mismatch')
        self._put('items', dict(row, vector=vector))
        return True

    @consistent_read
    def get_commit(self, commit_id): return self._one('commits', commit_id=commit_id)
    def put_commit(self, commit_id, fingerprint, receipt):
        state = self._locked_space()
        if not (commit_id.startswith('turn:') or commit_id.startswith('control:')) or len(commit_id) > 128: raise ValueError('Invalid commit identity')
        row = dict(commit_id=commit_id, space_id=self.space_id, fingerprint=digest(fingerprint), revision=state['revision'], receipt=json_copy(receipt))
        old = self.get_commit(commit_id)
        if old:
            if old['fingerprint'] != row['fingerprint'] or old['receipt'] != row['receipt']: raise ValueError('Commit identity conflict')
            return old
        self._put('commits', row)
        return deepcopy(row)

    @consistent_read
    def candidate_index_identity(self, message_id, role, *, model, dimension, completed_message=None):
        """Capture immutable computation identity without preparing an SQL target.

        A detached completed_message supports the final turn before its business
        transaction persists it. Apply still verifies the committed authority.
        """
        state = self.get_space()
        if state is None: raise ValueError('Memory space has not been initialized')
        message = deepcopy(completed_message) if completed_message is not None else self._message(message_id)
        if message.get('id') != message_id or message.get('space_id') != self.space_id or message.get('status') != 'completed': raise ValueError('Invalid candidate message identity')
        if type(model) is not str or not 0 < len(model) <= 128 or type(dimension) is not int or dimension < 1: raise ValueError('Invalid embedding target')
        iid = item_id_for(message_id, role)
        text = message['message'] if role == 'user' else message['response']['text']
        if type(text) is not str or not text.strip(): raise ValueError('Invalid candidate body')
        version, _, known = projection_provenance(message)
        if not known: raise ValueError('Unknown candidate provenance')
        old = self._one('items', item_id=iid)
        return dict(item_id=iid, checksum=digest(text), source_version=version, model=model, dimension=dimension,
            expected_revision=state['revision'], deletion_generation=state['deletion_generation'],
            expected_index_generation=old['index_generation'] if old else None)

    @consistent_read
    def build_mutation(self, *, project_message_ids=(), items=(), cards=(), checkpoint=None, index_results=()):
        """Export detached request results without changing persistent state."""
        state = self.get_space()
        if state is None: raise ValueError('Memory space has not been initialized')
        operations = [{'op': 'project_completed', 'message_id': mid} for mid in project_message_ids]
        operations.extend({'op': 'update_item', 'item': item.model_dump(mode='json') if isinstance(item, MemoryItem) else json_copy(item)} for item in items)
        operations.extend({'op': 'put_card', 'card': card.model_dump(mode='json') if isinstance(card, PlaybookCard) else json_copy(card)} for card in cards)
        for result in index_results:
            result = json_copy(result)
            if type(result) is not dict or set(result) != {'identity', 'vector'}: raise ValueError('Index result requires captured immutable identity')
            operations.append({'op': 'index_result', 'result': result})
        return MemoryMutation(expected_revision=state['revision'], deletion_generation=state['deletion_generation'],
            checkpoint=checkpoint, operations=json_copy(operations))

    def _index_result(self, result, prior, mutation):
        if type(result) is not dict or set(result) != {'identity', 'vector'}: raise ValueError('Invalid candidate index result')
        captured = result['identity']
        fields = {'item_id', 'checksum', 'source_version', 'model', 'dimension', 'expected_index_generation', 'expected_revision', 'deletion_generation'}
        if type(captured) is not dict or set(captured) != fields: raise ValueError('Invalid captured candidate identity')
        if captured['expected_revision'] != mutation.expected_revision or captured['deletion_generation'] != mutation.deletion_generation: raise ValueError('Stale candidate computation generation')
        row = self._one('items', item_id=captured['item_id'])
        if row is None or not self._valid_item(row) or captured['checksum'] != row['checksum'] or captured['source_version'] != row['source_version']:
            raise ValueError('Stale candidate index body/source')
        if captured['expected_index_generation'] != prior: raise ValueError('Stale candidate embedding target')
        identity = self.index_identity(captured['item_id'], model=captured['model'], dimension=captured['dimension'])
        if not self.complete_index(identity, result['vector']): raise ValueError('Candidate index identity rejected')

    @staticmethod
    def export_mutation(mutation):
        return json_copy(mutation.to_dict() if isinstance(mutation, MemoryMutation) else MemoryMutation.from_dict(mutation).to_dict())

    def apply_mutation(self, mutation):
        self._write()
        try:
            payload = self.export_mutation(mutation)
            mutation = MemoryMutation.from_dict(payload)
            if any(type(v) is not int or v < 0 for v in (mutation.expected_revision, mutation.deletion_generation)): raise ValueError('Invalid mutation counters')
            if type(mutation.operations) is not list: raise ValueError('Invalid operations')
            fields = {'project_completed': {'message_id'}, 'put_card': {'card'}, 'delete_card': {'card_id'},
                      'forget_item': {'item_id'}, 'invalidate_source': {'source_version'},
                      'dependencies': {'set_id', 'derived_id', 'records'}, 'checkpoint': {'checkpoint'},
                      'update_item': {'item'}, 'complete_index': {'identity', 'vector'}, 'index_result': {'result'}}
            for operation in mutation.operations:
                if type(operation) is not dict or operation.get('op') not in fields or set(operation) != fields[operation['op']] | {'op'}: raise ValueError('Unknown or malformed memory operation')
            with self._atomic():
                state = self._locked_space()
                if state['revision'] != mutation.expected_revision or state['deletion_generation'] != mutation.deletion_generation: raise ValueError('Memory revision/deletion generation conflict')
                prior_indexes = {}
                for op in mutation.operations:
                    if op['op'] == 'index_result':
                        iid = op['result']['identity']['item_id']
                        row = self._one('items', item_id=iid)
                        prior_indexes[iid] = row['index_generation'] if row else None
                for operation in mutation.operations:
                    args = {key: value for key, value in operation.items() if key != 'op'}
                    if operation['op'] == 'index_result':
                        self._index_result(args['result'], prior_indexes[args['result']['identity']['item_id']], mutation)
                        continue
                    method = {'dependencies': self.save_dependency_set, 'checkpoint': self.save_checkpoint}.get(operation['op']) or getattr(self, operation['op'])
                    if method(**args) is False: raise ValueError('Memory operation rejected')
                if mutation.checkpoint is not None: self.save_checkpoint(mutation.checkpoint)
                state = self.get_space()
                state['revision'] += 1
                self._put('spaces', state)
                return state['revision']
        except (ValueError, TypeError, KeyError, AttributeError, sa.exc.IntegrityError) as exc:
            raise InvalidMutation(str(exc)) from exc

    def reserve_call(self, call_id, message_id, run_id, attempt, runtime, budget):
        self._locked_space()
        message = self._business_message(message_id)
        if not message or message['space_id'] != self.space_id or message['run_id'] != run_id: raise ValueError('Invalid call message/run')
        if type(attempt) is not int or attempt < 0 or not runtime or len(runtime) > 64: raise ValueError('Invalid call identity')
        budget = self._usage(budget)
        row = dict(call_id=call_id, space_id=self.space_id, message_id=message_id, run_id=run_id, attempt=attempt,
            runtime=runtime, status='reserved', budget=budget, usage={}, possibly_sent=False)
        old = self._one('calls', call_id=call_id)
        if old:
            if any(old[key] != row[key] for key in ('message_id', 'run_id', 'attempt', 'runtime', 'budget')): raise ValueError('Call identity conflict')
            return deepcopy(old)
        self._put('calls', row)
        return deepcopy(row)

    @staticmethod
    def _usage(value):
        value = json_copy(value)
        if type(value) is not dict or not set(value) <= {'input_tokens', 'output_tokens', 'total_tokens', 'cost', 'max_tokens', 'max_cost'} or any(type(v) not in (int, float) or v < 0 for v in value.values()): raise ValueError('Usage requires safe nonnegative numeric counters')
        return value

    def transition_call(self, call_id, status, usage=None):
        self._locked_space()
        row = self._one('calls', call_id=call_id)
        transitions = {'reserved': {'sent', 'failed'}, 'sent': {'succeeded', 'failed', 'unknown'}}
        if row is None or status not in transitions.get(row['status'], set()): raise ValueError('Invalid call transition')
        self._put('calls', dict(row, status=status, usage=self._usage(usage or {}), possibly_sent=row['possibly_sent'] or status == 'sent'))


class InMemoryMemoryRepository(MemoryRepository):
    def __init__(self, materials, space_id): self.materials, self.space_id = materials, space_id
    def _read_guard(self): return self.materials._lock
    @property
    def active(self): return self.materials._transaction_active.get()
    @contextmanager
    def transaction(self):
        if self.active:
            yield
        else:
            with self.materials.transaction(): yield
    @consistent_read
    def _business_space(self): return self.space_id in self.materials.learning_spaces
    @consistent_read
    def _business_message(self, mid): return deepcopy(self.materials.assessment_data['messages'].get(mid))
    @consistent_read
    def _business_conversation(self, cid): return self.materials.assessment_data['conversations'].get(cid, {}).get('space_id') == self.space_id
    @consistent_read
    def _rows(self, key, _limit=None, **filters):
        filters['space_id'] = self.space_id
        result = []
        for row in self.materials.agent_memory_data[key].values():
            if all(row.get(k) == v for k, v in filters.items()):
                result.append(deepcopy(row))
                if _limit is not None and len(result) >= _limit: break
        return result
    @consistent_read
    def _one(self, key, **filters):
        rows = self._rows(key, **filters)
        return rows[0] if rows else None
    def _lock_space(self): return self.get_space()
    def _put(self, key, row):
        self._write()
        pk = list(TABLES[key].primary_key.columns)[0].name
        old = self.materials.agent_memory_data[key].get(row[pk])
        if old and old['space_id'] != self.space_id: raise ValueError('Identity belongs to another space')
        self.materials.agent_memory_data[key][row[pk]] = deepcopy(row)
    def _delete(self, key, **filters):
        self._write()
        pk = list(TABLES[key].primary_key.columns)[0].name
        for row in self._rows(key, **filters): self.materials.agent_memory_data[key].pop(row[pk])
    @contextmanager
    def _atomic(self):
        previous = deepcopy(self.materials.agent_memory_data)
        try: yield
        except BaseException:
            self.materials.agent_memory_data = previous
            raise


class SqlAlchemyMemoryRepository(MemoryRepository):
    def __init__(self, unit_of_work, space_id): self.unit_of_work, self.space_id = unit_of_work, space_id
    @property
    def active(self): return self.unit_of_work.active
    def transaction(self): return self.unit_of_work.transaction()
    def _business_space(self):
        from knowpath_backend.learning.persistence.db import LearningSpaceRow
        with self.unit_of_work.session() as session: return session.get(LearningSpaceRow, self.space_id) is not None
    def _business_message(self, mid):
        with self.unit_of_work.session() as session:
            row = session.get(LearningMessageRow, mid)
            return {c.name: deepcopy(getattr(row, c.name)) for c in row.__table__.columns} if row else None
    def _business_conversation(self, cid):
        with self.unit_of_work.session() as session:
            return session.scalar(sa.select(ConversationRow.id).where(ConversationRow.id == cid, ConversationRow.space_id == self.space_id)) is not None
    def _rows(self, key, _limit=None, **filters):
        table = TABLES[key]
        filters['space_id'] = self.space_id
        with self.unit_of_work.session() as session:
            query = sa.select(table).filter_by(**filters)
            if _limit is not None: query = query.limit(_limit)
            return [deepcopy(dict(row)) for row in session.execute(query).mappings()]
    def _one(self, key, **filters):
        rows = self._rows(key, **filters)
        return rows[0] if rows else None
    def _lock_space(self):
        with self.unit_of_work.session() as session:
            row = session.execute(sa.select(TABLES['spaces']).where(TABLES['spaces'].c.space_id == self.space_id).with_for_update()).mappings().first()
            return dict(row) if row else None
    def _put(self, key, row):
        self._write()
        table = TABLES[key]
        pk = list(table.primary_key.columns)[0]
        with self.unit_of_work.session() as session:
            old = session.execute(sa.select(table.c.space_id).where(pk == row[pk.name])).scalar_one_or_none()
            if old is not None and old != self.space_id: raise ValueError('Identity belongs to another space')
            if old is None: session.execute(sa.insert(table).values(**deepcopy(row)))
            else: session.execute(sa.update(table).where(pk == row[pk.name], table.c.space_id == self.space_id).values(**deepcopy(row)))
    def _delete(self, key, **filters):
        self._write()
        with self.unit_of_work.session() as session:
            session.execute(sa.delete(TABLES[key]).filter_by(space_id=self.space_id, **filters))
    @contextmanager
    def _atomic(self):
        with self.unit_of_work.session() as session:
            with session.begin_nested(): yield


class SqlAlchemyCallLedger:
    """Safe call accounting in independent short transactions.

    The runner passes only identity and numeric budget/usage. Never call while
    holding a business transaction: that can invert database lock order. The
    independent Session is private and is never handed to a model candidate.
    """
    def __init__(self, business_unit_of_work, space_id):
        from knowpath_backend.learning.persistence.unit_of_work import SqlAlchemyUnitOfWork
        self._business = business_unit_of_work
        self._repository = SqlAlchemyMemoryRepository(SqlAlchemyUnitOfWork(business_unit_of_work.engine), space_id)
    def _guard(self):
        if self._business.active: raise RuntimeError('Call ledger requires no active business transaction')
    def reserve(self, call_id, message_id, run_id, attempt, runtime, budget):
        self._guard()
        with self._repository.transaction():
            return self._repository.reserve_call(call_id, message_id, run_id, attempt, runtime, budget)
    def transition(self, call_id, status, usage=None):
        self._guard()
        with self._repository.transaction(): self._repository.transition_call(call_id, status, usage)
    def get(self, call_id):
        self._guard()
        return self._repository._one('calls', call_id=call_id)
