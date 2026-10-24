"""Request-local copies of the generic memory manager. No provider I/O on load."""
from copy import deepcopy
from dataclasses import replace

from knowpath_backend.memory import MemoryConfig, MemoryManager
from knowpath_backend.memory.backends.base import ColdBackend
from knowpath_backend.memory.backends.inmemory import InMemoryVectorBackend
from knowpath_backend.memory.contracts import MemoryMutation, MemoryCheckpoint
from knowpath_backend.memory.item import MemoryItem
from knowpath_backend.memory.playbook.store import PlaybookStore
from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
from knowpath_backend.learning.workers.model_tasks import LeaseLost
from knowpath_backend.learning.agent_memory.repository import digest


class MemoryLease:
    """Short transactions hold only the memory-space execution lock."""
    def __init__(self, repository, message_id, owner, *, seconds=30):
        self.repository, self.message_id, self.owner = repository, message_id, owner
        self.seconds, self.token = seconds, None

    def acquire(self):
        with self.repository.transaction():
            receipt = self.repository.acquire_execution_lease(self.message_id, self.owner, seconds=self.seconds)
        self.token = receipt['lease_token'] if receipt else None
        return receipt is not None

    def renew(self):
        with self.repository.transaction():
            return self.repository.renew_execution_lease(self.token, self.message_id, self.owner, seconds=self.seconds)

    def check(self):
        if not self.repository.check_execution_lease(self.token, self.message_id, self.owner): raise LeaseLost()

    def release(self):
        with self.repository.transaction():
            if self.repository.get_space() is None: return False
            return self.repository.release_execution_lease(self.token, self.message_id, self.owner)


class LocalColdBackend(ColdBackend):
    def __init__(self, items, loader=None):
        self.items = {item.id: item for item in items}
        self.loader = loader
    def add(self, item): self.items[item.id] = item
    def get(self, item_id):
        if item_id not in self.items and self.loader: self.loader(item_id)
        return self.items.get(item_id)
    def iter_all(self):
        if self.loader: self.loader(None)
        return iter(self.items.values())


class LocalVectorBackend(InMemoryVectorBackend):
    def __init__(self):
        super().__init__()
        self.loader = None
    def get(self, item_id):
        if item_id not in self._items and self.loader: self.loader(item_id)
        return super().get(item_id)
    def items(self):
        if self.loader: self.loader(None)
        return super().items()
    def search(self, *args, **kwargs):
        if self.loader: self.loader(None)
        return super().search(*args, **kwargs)


class MemorySession:
    def __init__(self, repository, manager, state, identity, items, cards):
        self.repository, self.manager = repository, manager
        self.state, self.identity = state, identity
        self.original_items = items if isinstance(items, dict) else {item.id: item.model_dump(mode='json') for item in items}
        self.original_cards = {card.id: card.model_dump(mode='json') for card in cards}
        self.closed = False

    def publish(self, message_id, lease, mutation):
        """Join the caller's publication transaction after all business locks.

        The caller must persist its completed authoritative message first. A
        receipt makes uncertain-commit recovery safe before any model replay.
        """
        repo = self.repository
        repo._write()
        old = repo.get_commit('turn:' + message_id)
        if old is not None: return old['receipt']
        if lease.repository is not repo or lease.message_id != message_id: raise LeaseLost()
        lease.check()
        # Recheck inside the mutation boundary, then fence with a conditional
        # lease update after applying; an expired lease rolls back everything.
        with repo._atomic():
            # All business/source locks are already owned by the caller. Fence
            # memory-only revocations here without reacquiring those locks.
            repo._locked_space()
            if hasattr(self, 'publication_memory') and self.memory_references(self.publication_closure, current=True) != self.publication_memory:
                raise LeaseLost('Consumed memory changed before publication')
            revision = repo.apply_mutation(mutation)
            if not repo.renew_execution_lease(lease.token, message_id, lease.owner, seconds=lease.seconds):
                raise LeaseLost()
            receipt = dict(message_id=message_id, revision=revision,
                deletion_generation=mutation.deletion_generation)
            repo.put_commit('turn:' + message_id, mutation.to_dict(), receipt)
        return receipt

    def freeze(self, *, project_message_ids=()):
        if self.closed: raise RuntimeError('Memory session is closed')
        # Read the captured generation, never refresh it after model execution.
        cp = self.manager.export_checkpoint(**self.identity, revision=self.state['revision'] + 1)
        items = dict(self.manager.cold.backend.items)
        items.update(self.manager.semantic.backend._items)
        items.update({item.id: item for item in self.manager.working.items()})
        operations = [{'op': 'project_completed', 'message_id': mid} for mid in project_message_ids]
        for iid, original in self.original_items.items():
            item = items.get(iid)
            if item is not None and item.model_dump(mode='json') != original:
                operations.append({'op': 'update_item', 'item': item.model_dump(mode='json')})
        for card in self.manager.playbook._memory_cache:
            value = card.model_dump(mode='json')
            if self.original_cards.get(card.id) != value:
                operations.append({'op': 'put_card', 'card': value})
        return MemoryMutation(expected_revision=self.state['revision'],
            deletion_generation=self.state['deletion_generation'], checkpoint=cp, operations=operations)

    def publication_dependencies(self, dependencies, *, current_message_id):
        """Detach the complete source closure before taking publication locks.

        References stay in the result for final authoritative qualification;
        expanded material/message identities determine the ordered lock set.
        """
        from knowpath_backend.learning.agent_memory.scope import complete_dependencies
        from knowpath_backend.learning.agent_memory.repository import projection_provenance, item_id_for
        result, visited = [], set()
        pending = list(deepcopy(dependencies))
        new_items = {item_id_for(current_message_id, role) for role in ('user', 'assistant')}
        def inherit(value):
            metadata = value.get('metadata', {}) if isinstance(value, dict) else value.metadata
            for dependency in complete_dependencies(value, self.repository.get_dependency_set):
                if dependency.get('item_id') in new_items:
                    dependency = {key: field for key, field in dependency.items() if key != 'item_id'}
                if dependency: pending.append(dependency)
            pending.extend({'item_id': iid} for iid in metadata.get('source_item_ids', []) if iid not in new_items)
        mutation = getattr(self, 'prepared_mutation', None)
        if mutation is not None:
            cp = mutation.checkpoint.state
            values = [*cp['l1'], *cp['l2']['buffer']]
            if cp['l2']['summary']: values.append(cp['l2']['summary'])
            values.extend(op['item'] for op in mutation.operations if op['op'] == 'update_item')
            values.extend(op['card'] for op in mutation.operations if op['op'] == 'put_card')
            for value in values:
                source_message = value.get('metadata', {}).get('source_message_id')
                if source_message and source_message != current_message_id:
                    pending.append({'message_id': source_message})
                if value.get('metadata', {}).get('source_message_id') == current_message_id:
                    # The new projection's complete canonical set is local;
                    # its external set is minted atomically by project_completed.
                    value = deepcopy(value)
                    value['metadata'].pop('source_ref', None)
                inherit(value)
        while pending:
            dep = pending.pop()
            if dep in result: continue
            result.append(dep)
            refs = [('item', dep.get('item_id')), ('message', dep.get('message_id'))]
            if dep.get('kind') == 'card': refs.append(('card', dep.get('id')))
            for kind, identifier in refs:
                if not identifier or (kind == 'message' and identifier == current_message_id): continue
                key = (kind, identifier)
                if key in visited: continue
                visited.add(key)
                if kind == 'message':
                    row = self.repository._business_message(identifier)
                    if row is None: raise ValueError('Consumed message is unavailable')
                    value = {'metadata': projection_provenance(row)[1]}
                    value['metadata'].pop('source_ref', None)
                else:
                    value = (self.repository.get_item(identifier) if kind == 'item'
                             else self.repository.get_card(identifier))
                    if value is None: raise ValueError('Consumed memory is unavailable')
                    source_message = value.metadata.get('source_message_id')
                    if source_message and source_message != current_message_id:
                        pending.append({'message_id': source_message})
                inherit(value)
        return result

    def memory_references(self, dependencies, *, current=False):
        """Read only memory identities, safe under the final memory lock."""
        result = {}
        for dep in dependencies:
            kind = 'card' if dep.get('kind') == 'card' else 'item'
            identifier = dep.get('id') if kind == 'card' else dep.get('item_id')
            if not identifier: continue
            uow = getattr(self.repository, 'unit_of_work', None)
            if current and uow is not None:
                # A locking read bypasses an earlier MySQL repeatable-read
                # snapshot. The memory-space lock prevents further revocation.
                import sqlalchemy as sa
                from knowpath_backend.learning.agent_memory.models import TABLES
                table = TABLES['cards' if kind == 'card' else 'items']
                with uow.session() as sql:
                    row = sql.execute(sa.select(table).filter_by(space_id=self.repository.space_id,
                        **{('card_id' if kind == 'card' else 'item_id'): identifier}).with_for_update()).mappings().first()
                if row is None:
                    result[(kind, identifier)] = None
                    continue
                from knowpath_backend.memory.playbook.card import PlaybookCard
                value = (PlaybookCard if kind == 'card' else MemoryItem).model_validate(row['payload'])
                result[(kind, identifier)] = value.model_dump(mode='json')
                continue
            value = self.repository.get_card(identifier) if kind == 'card' else self.repository.get_item(identifier)
            result[(kind, identifier)] = value.model_dump(mode='json') if value is not None else None
        return result

    def close(self):
        if not self.closed:
            self.closed = True
            self.manager.close()


class MemorySessionService:
    def __init__(self, repository): self.repository = repository

    def load(self, conversation_id, context_epoch, fingerprint, *, eligible, config=None, **manager_options):
        allowed = {'embedding', 'summarizer', 'summary_flush_threshold', 'reconciler', 'conflict_detector', 'llm'}
        if not set(manager_options) <= allowed:
            raise ValueError('Cannot override Web memory persistence or context boundaries')
        config = config or MemoryConfig(tick_mode='sync', conflict_strength='off',
            l2_reflect_every_n_turns=0, kg_reconcile_every_n_turns=0)
        if config.storage_dir is not None or config.user_storage_dir is not None or config.tick_mode == 'async':
            raise ValueError('Web working copies require local memory and synchronous maintenance')
        repo = self.repository
        # A short consistent transaction captures all layers and counters. It
        # ends before constructing the manager or invoking any model adapter.
        with repo.transaction():
            state = repo._locked_space()
            cards = repo.list_cards()
            checkpoint = repo.get_checkpoint(conversation_id, context_epoch)
            references = set()
            if checkpoint is not None:
                references.update(value['id'] for value in checkpoint.state['l1'])
                references.update(value['id'] for value in checkpoint.state['l2']['buffer'])
                if checkpoint.state['l2']['summary']:
                    references.add(checkpoint.state['l2']['summary']['id'])
                references.update(value['item_id'] for value in checkpoint.state['l3'])
            items = [item for iid in references if (item := repo.get_item(iid)) is not None]
            for item in list(items):
                for peer in repo.items_for_turn(item.turn_id):
                    if peer.id not in {value.id for value in items}: items.append(peer)
            vectors = {item.id: deepcopy(repo._one('items', item_id=item.id)['vector']) for item in items}
        # Source authorization may acquire business/source locks. Never invoke
        # it while holding the memory-space lock. These detached copies retain
        # the captured generation; publication rejects any intervening deletion.
        cards = [card for card in cards if eligible(card, 'L0')]
        items = [item for item in items if eligible(item, 'L4')]
        cold = LocalColdBackend(items)
        vector = LocalVectorBackend()
        originals = {item.id: item.model_dump(mode='json') for item in items}
        for item in items:
            if eligible(item, 'L5'): vector.add(item, vectors[item.id])
        loaded_all = False
        def load_items(item_id):
            nonlocal loaded_all
            if item_id is None and loaded_all: return
            with repo.transaction():
                current = repo._locked_space()
                if any(current[key] != state[key] for key in ('revision', 'deletion_generation')):
                    raise LeaseLost('Memory changed during request-local loading')
                if item_id is None:
                    fetched = repo.list_items()
                else:
                    found = repo.get_item(item_id)
                    fetched = repo.items_for_turn(found.turn_id) if found else []
                rows = [(item, deepcopy(repo._one('items', item_id=item.id)['vector'])) for item in fetched]
            for item, embedding in rows:
                if not eligible(item, 'L4'): continue
                if item.id in cold.items: continue
                cold.add(item)
                originals[item.id] = item.model_dump(mode='json')
                if eligible(item, 'L5'): vector.add(item, embedding)
            if item_id is None: loaded_all = True
        cold.loader = load_items
        vector.loader = load_items
        store = PlaybookStore()
        for card in cards: store.add(card)
        config = config or MemoryConfig(tick_mode='sync', conflict_strength='off',
            l2_reflect_every_n_turns=0, kg_reconcile_every_n_turns=0)
        if config.storage_dir is not None or config.user_storage_dir is not None or config.tick_mode == 'async':
            raise ValueError('Web working copies require local memory and synchronous maintenance')
        manager = MemoryManager(config, cold_backend=cold, vector_backend=vector,
            card_store=store, context_policy=WebContextPolicy(), **manager_options)
        identity = dict(space_id=repo.space_id, conversation_id=conversation_id,
            context_epoch=context_epoch, context_fingerprint=fingerprint)
        try:
            if checkpoint is not None:
                if checkpoint.context_fingerprint != fingerprint or checkpoint.revision > state['revision']:
                    raise ValueError('Checkpoint identity or revision is inconsistent')
                data = deepcopy(checkpoint.state)
                for layer, purpose in [('l1', 'L1')]:
                    data[layer] = [value for value in data[layer] if eligible(MemoryItem.model_validate(value), purpose)]
                data['l2']['buffer'] = [value for value in data['l2']['buffer'] if eligible(MemoryItem.model_validate(value), 'L2')]
                if data['l2']['summary'] and not eligible(MemoryItem.model_validate(data['l2']['summary']), 'L2'):
                    data['l2']['summary'] = None
                data['l3'] = [value for value in data['l3'] if value['item_id'] in cold.items and eligible(cold.items[value['item_id']], 'L3')]
                manager.restore_checkpoint(replace(checkpoint, state=data))
                # Use the same canonical hot instances in L4 and L5.
                for item in manager.semantic._hot_items.values():
                    if item.id in cold.items:
                        cold.add(item)
                    if vector.get(item.id) is not None:
                        vector._items[item.id] = item
            return MemorySession(repo, manager, state, identity, originals, cards)
        except BaseException:
            manager.close()
            raise
