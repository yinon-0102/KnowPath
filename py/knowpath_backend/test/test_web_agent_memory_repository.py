"""Shared SQL/memory persistence contract; never calls a model provider."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, update, delete
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.learning.persistence.db import Base, LearningSpaceRow, ConversationRow, LearningMessageRow
from knowpath_backend.learning.persistence.unit_of_work import SqlAlchemyUnitOfWork
from knowpath_backend.memory.contracts import MemoryMutation, MemoryCheckpoint


@pytest.fixture(params=['memory', 'sqlite'])
def store(request, tmp_path):
    from knowpath_backend.learning.agent_memory.repository import InMemoryMemoryRepository, SqlAlchemyMemoryRepository
    sid, cid, mid = str(uuid4()), str(uuid4()), str(uuid4())
    now = datetime.now(timezone.utc)
    message = dict(id=mid, space_id=sid, conversation_id=cid, run_id=str(uuid4()), status='completed',
                   message='original question', response={'text': 'original answer'}, sequence=1,
                   snapshot={'scope_version': 1, 'request_scope': {'space_id': sid, 'scope_version': 1,
                       'request_scope_id': 'frozen-scope', 'bindings': [{'material_id': 'mat', 'material_version_id': 'v1', 'graph_version': 1}], 'material_ids': ['mat']},
                       'messages': [{'content': 'never use prompt snapshots'}]}, created_at=now)
    if request.param == 'memory':
        owner = InMemoryMaterialRepository()
        owner.learning_spaces[sid] = {'id': sid}
        owner.assessment_data['conversations'][cid] = dict(id=cid, space_id=sid)
        owner.assessment_data['messages'][mid] = copy.deepcopy(message)
        repo = InMemoryMemoryRepository(owner, sid)
        def change(**values): owner.assessment_data['messages'][mid].update(copy.deepcopy(values))
        def corrupt(iid): owner.agent_memory_data['items'][iid]['content'] = 'drift'
    else:
        engine = create_engine('sqlite+pysqlite:///' + str(tmp_path / 'memory.db'))
        event.listen(engine, 'connect', lambda conn, _: conn.execute('PRAGMA foreign_keys=ON'))
        Base.metadata.create_all(engine)
        uow = SqlAlchemyUnitOfWork(engine)
        with uow.transaction(), uow.session() as session:
            session.add(LearningSpaceRow(id=sid, name='test', status='active', bindings=[], topic_ids=[], excluded_topic_ids=[], created_at=now, updated_at=now))
            session.flush()
            session.add(ConversationRow(id=cid, space_id=sid, created_at=now))
            session.flush()
            session.add(LearningMessageRow(**message))
        repo = SqlAlchemyMemoryRepository(uow, sid)
        def change(**values):
            with uow.transaction(), uow.session() as session:
                session.execute(update(LearningMessageRow).where(LearningMessageRow.id == mid).values(**values))
        def corrupt(iid):
            from knowpath_backend.learning.agent_memory.models import TABLES
            with engine.begin() as conn:
                conn.execute(update(TABLES['items']).where(TABLES['items'].c.item_id == iid).values(content='drift'))
    with repo.transaction(): repo.ensure_space()
    yield repo, mid, cid, change, corrupt


def test_completed_projection_stable_unique_and_detached(store):
    from knowpath_backend.learning.agent_memory.repository import item_id_for
    repo, mid, _, _, _ = store
    with repo.transaction():
        repo.project_completed(mid)
        repo.project_completed(mid)
    items = repo.items_for_turn(mid)
    assert len(items) == 2
    assert {i.id for i in items} == {item_id_for(mid, role) for role in ('user', 'assistant')}
    assert {i.turn_id for i in items} == {mid}
    assert {i.content for i in items} == {'original question', 'original answer'}
    items[0].content = 'mutated read'
    assert repo.get_item(items[0].id).content != 'mutated read'


def test_writes_require_external_active_and_full_business_rollback(store):
    repo, mid, _, _, _ = store
    with pytest.raises(RuntimeError, match='active'): repo.project_completed(mid)
    with pytest.raises(RuntimeError, match='boom'):
        with repo.transaction():
            repo.project_completed(mid)
            raise RuntimeError('boom')
    assert repo.items_for_turn(mid) == []
    with repo.transaction(): repo.project_completed(mid)
    assert len(repo.items_for_turn(mid)) == 2


def test_memory_active_is_context_local(store):
    repo, mid, _, _, _ = store
    if not hasattr(repo, 'materials'): pytest.skip('memory context contract')
    with repo.transaction():
        def other_context():
            with pytest.raises(RuntimeError, match='active'): repo.project_completed(mid)
        with ThreadPoolExecutor(max_workers=1) as pool: pool.submit(other_context).result(timeout=3)


def test_projection_repair_is_explicit_and_never_revives_tombstones(store):
    repo, mid, _, change, corrupt = store
    with repo.transaction(): repo.project_completed(mid)
    iid = repo.items_for_turn(mid)[0].id
    corrupt(iid)
    assert repo.get_item(iid) is None  # read quarantines logically, without writing
    with repo.transaction(): repo.repair_item(iid)
    assert repo.get_item(iid).content in {'original question', 'original answer'}
    with repo.transaction(): repo.forget_item(iid)
    with repo.transaction(): repo.project_completed(mid); repo.repair_item(iid)
    assert repo.get_item(iid) is None
    change(status='failed', response=None)
    assert repo.items_for_turn(mid) == []


def test_invalid_authoritative_body_is_never_replaced_by_snapshot(store):
    repo, mid, _, change, _ = store
    change(response={'other': 'not an answer'})
    with repo.transaction(), pytest.raises(ValueError): repo.project_completed(mid)
    assert repo.items_for_turn(mid) == []


def test_mutation_revision_validation_atomic_even_when_error_caught(store):
    repo, mid, _, _, _ = store
    from knowpath_backend.learning.agent_memory.repository import InvalidMutation
    with repo.transaction():
        for mutation in (
            MemoryMutation(operations=[{'op': 'project_completed', 'message_id': mid}, {'op': 'unknown'}]),
            MemoryMutation(expected_revision=44),
            MemoryMutation(deletion_generation=44),
            MemoryMutation(operations=[{'op': 'put_card', 'card': {'confidence': float('nan')}}]),
        ):
            with pytest.raises(InvalidMutation): repo.apply_mutation(mutation)
        assert repo.items_for_turn(mid) == []
        assert repo.get_space()['revision'] == 0
        mutation = MemoryMutation(operations=[{'op': 'project_completed', 'message_id': mid}])
        assert repo.export_mutation(mutation) == mutation.to_dict()
        repo.apply_mutation(mutation)
    assert repo.get_space()['revision'] == 1


def test_mutation_runtime_failure_rolls_back_earlier_operations(store):
    repo, mid, _, _, _ = store
    from knowpath_backend.learning.agent_memory.repository import InvalidMutation
    with repo.transaction():
        with pytest.raises(InvalidMutation):
            repo.apply_mutation(MemoryMutation(operations=[{'op': 'project_completed', 'message_id': mid},
                {'op': 'project_completed', 'message_id': str(uuid4())}]))
        assert repo.items_for_turn(mid) == []


def test_dependency_graph_checkpoint_atomic_and_reverse_lookup(store):
    repo, mid, cid, _, _ = store
    records = [{'message_id': mid}, {'material_version_id': 'version-1', 'chunk_id': 'chunk-1'}]
    cp = MemoryCheckpoint(space_id=repo.space_id, conversation_id=cid, state={'dependency_set_ids': ['set-1']})
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.save_dependency_set('set-1', 'summary-1', records)
            repo.save_checkpoint(cp)
            raise RuntimeError('rollback')
    assert repo.get_dependency_set('set-1') is None
    assert repo.get_checkpoint(cid, 0) is None
    with repo.transaction():
        repo.save_dependency_set('set-1', 'summary-1', records)
        repo.save_checkpoint(cp)
    assert repo.get_dependency_set('set-1') == records
    assert repo.dependents({'chunk_id': 'chunk-1'}) == ['summary-1']
    with repo.transaction(), pytest.raises(ValueError):
        repo.save_checkpoint(MemoryCheckpoint(space_id=repo.space_id, conversation_id=cid, state={'dependency_set_ids': ['missing']}))


def test_index_result_requires_exact_body_source_generation_and_target(store):
    repo, mid, _, change, corrupt = store
    with repo.transaction(): repo.project_completed(mid)
    iid = repo.items_for_turn(mid)[0].id
    with repo.transaction(): identity = repo.index_identity(iid, model='embed-a', dimension=2)
    with repo.transaction(): assert repo.complete_index(identity, [0.1, 0.2])
    with repo.transaction(): repo.index_identity(iid, model='embed-b', dimension=2)
    with repo.transaction(): assert not repo.complete_index(identity, [0.1, 0.2])
    with repo.transaction(): newer = repo.index_identity(iid, model='embed-b', dimension=2)
    corrupt(iid)
    with repo.transaction(): assert not repo.complete_index(newer, [0.1, 0.2]); repo.repair_item(iid)
    with repo.transaction(): assert not repo.complete_index(newer, [0.1, 0.2])
    with repo.transaction(): fresh = repo.index_identity(iid, model='embed-b', dimension=2); repo.forget_item(iid)
    with repo.transaction(): assert not repo.complete_index(fresh, [0.1, 0.2])


def test_adapters_deny_candidate_body_writes_and_return_detached_cards(store):
    from knowpath_backend.learning.agent_memory.backends import RepositoryColdBackend, RepositoryVectorBackend, RepositoryPlaybookStore
    from knowpath_backend.memory.item import MemoryItem
    from knowpath_backend.memory.playbook.card import PlaybookCard
    repo, mid, _, _, _ = store
    cold, vector, cards = RepositoryColdBackend(repo), RepositoryVectorBackend(repo), RepositoryPlaybookStore(repo)
    with pytest.raises(RuntimeError): cold.add(MemoryItem(content='candidate'))
    card = PlaybookCard(id='card-1', content='prefer concise', type='preference', source='user_explicit')
    with pytest.raises(RuntimeError): cards.add(card)
    with repo.transaction():
        repo.project_completed(mid)
        cards.add(card)
    card.content = 'leak'
    assert cards.get('card-1').content == 'prefer concise'
    assert len(list(cold.iter_all())) == 2
    assert vector.items() == []
    with repo.transaction(): cards.remove('card-1')
    assert cards.get('card-1') is None


def test_call_ledger_allows_multiple_physical_calls_and_idempotent_reservation(store):
    repo, mid, _, _, _ = store
    run_id = repo._business_message(mid)['run_id']
    one, two = str(uuid4()), str(uuid4())
    with repo.transaction():
        repo.reserve_call(one, mid, run_id, 0, 'agent', {'max_tokens': 100})
        repo.reserve_call(one, mid, run_id, 0, 'agent', {'max_tokens': 100})
        repo.reserve_call(two, mid, run_id, 0, 'agent', {'max_tokens': 100})
        with pytest.raises(ValueError): repo.reserve_call(one, mid, run_id, 0, 'agent', {'max_tokens': 200})
        repo.transition_call(one, 'sent')
        repo.transition_call(one, 'succeeded', {'input_tokens': 4, 'output_tokens': 6})
        with pytest.raises(ValueError): repo.transition_call(one, 'sent')
        with pytest.raises(ValueError): repo.transition_call(two, 'sent', {'api_key': 'secret'})
    assert len(repo._rows('calls')) == 2


def test_vector_search_model_filtering_limits_and_direct_get(store):
    from knowpath_backend.learning.agent_memory.backends import RepositoryVectorBackend
    repo, mid, _, _, _ = store
    with repo.transaction(): repo.project_completed(mid)
    items = sorted(repo.items_for_turn(mid), key=lambda i: i.role)
    backend = RepositoryVectorBackend(repo, model='embed', dimension=2)
    with repo.transaction():
        backend.add(items[0], [1, 0]); backend.add(items[1], [0, 1])
    assert backend.search('ignored', [1, 0], k=1)[0][0].id == items[0].id
    filtered = RepositoryVectorBackend(repo, model='embed', dimension=2, predicate=lambda i: i.id == items[1].id)
    assert filtered.search('', [1, 0], k=1)[0][0].id == items[1].id
    assert RepositoryVectorBackend(repo, model='other', dimension=2).items() == []
    with pytest.raises(ValueError): backend.search('', [float('nan'), 0])
    with pytest.raises(ValueError): backend.search('', [1])
    with repo.transaction(), pytest.raises(ValueError): backend.add(items[0], [1])
    limited = RepositoryVectorBackend(repo, model='embed', dimension=2, scan_limit=1, predicate=lambda i: False)
    with pytest.raises(ValueError, match='scan'): limited.search('', [1, 0])
    # ID restoration is exact even if a search would exceed the scan budget.
    assert limited.get(items[0].id) is None  # scope predicate still applies
    direct = RepositoryVectorBackend(repo, model='embed', dimension=2, scan_limit=1)
    assert direct.get(items[0].id).id == items[0].id


def test_repair_restores_payload_corruption_and_updated_provenance(store):
    repo, mid, _, change, _ = store
    with repo.transaction(): repo.project_completed(mid)
    item = repo.items_for_turn(mid)[0]
    row = repo._one('items', item_id=item.id)
    row['payload']['content'] = 'payload drift'
    with repo.transaction(): repo._put('items', row)
    assert repo.get_item(item.id) is None
    with repo.transaction(): repo.repair_item(item.id)
    assert repo.get_item(item.id).content == item.content
    change(response={'text': 'original answer', 'source_dependencies': [{'chunk_id': 'new-chunk'}]})
    assert repo.get_item(item.id) is None
    with repo.transaction(): repo.repair_item(item.id)
    assert {'chunk_id': 'new-chunk'} in repo.get_item(item.id).metadata['source_dependencies']
    version = repo._one('items', item_id=item.id)['source_version']
    with repo.transaction(): repo.invalidate_source(version); repo.repair_item(item.id)
    assert repo.get_item(item.id) is None


def test_unknown_scope_is_saved_as_quarantined_without_invented_provenance(store):
    repo, mid, _, change, _ = store
    change(snapshot={'history': [{'content': 'unsafe'}]})
    with repo.transaction(): repo.project_completed(mid)
    assert repo.items_for_turn(mid) == []
    assert {r['lifecycle'] for r in repo._rows('items')} == {'quarantined'}


def test_working_state_mutation_and_new_projection_vector_are_atomic(store):
    from knowpath_backend.learning.agent_memory.repository import item_id_for, projection_provenance, digest
    from knowpath_backend.memory.item import MemoryItem
    repo, mid, _, _, _ = store
    row = repo._business_message(mid)
    source_version, metadata, _ = projection_provenance(row)
    iid = item_id_for(mid, 'user')
    # Prepared entirely from a detached completed message; no SQL target write.
    result = dict(identity=repo.candidate_index_identity(mid, 'user', model='embed', dimension=2), vector=[1, 0])
    mutation = repo.build_mutation(project_message_ids=[mid], index_results=[result])
    assert repo._rows('items') == []
    with repo.transaction(): repo.apply_mutation(mutation)
    item = repo.get_item(iid)
    item.touch(); item.pinned = True
    mutation = repo.build_mutation(items=[item])
    with repo.transaction(): repo.apply_mutation(mutation)
    assert repo.get_item(iid).access_count == 1 and repo.get_item(iid).pinned
    item.content = 'candidate overwrite'
    from knowpath_backend.learning.agent_memory.repository import InvalidMutation
    with repo.transaction(), pytest.raises(InvalidMutation): repo.apply_mutation(repo.build_mutation(items=[item]))
    assert repo.get_item(iid).content == row['message']


def test_new_projection_bad_vector_rolls_back_targets_and_items(store):
    from knowpath_backend.learning.agent_memory.repository import item_id_for, projection_provenance, digest, InvalidMutation
    repo, mid, _, _, _ = store
    message = repo._business_message(mid)
    result = dict(identity=repo.candidate_index_identity(mid, 'user', model='embed', dimension=2), vector=[1])
    mutation = repo.build_mutation(project_message_ids=[mid], index_results=[result])
    with repo.transaction():
        with pytest.raises(InvalidMutation): repo.apply_mutation(mutation)
        assert repo._rows('items') == []
        assert repo.get_space()['revision'] == 0


def test_projection_registers_complete_reverse_dependencies_and_selection_identity(store):
    from knowpath_backend.learning.agent_memory.repository import projection_provenance
    repo, mid, _, change, _ = store
    message = repo._business_message(mid)
    message['snapshot']['request_scope']['mode'] = 'all'
    change(snapshot=message['snapshot'])
    with repo.transaction(): repo.project_completed(mid)
    item = repo.items_for_turn(mid)[0]
    assert repo.dependents({'material_version_id': 'v1'}) == [mid]
    set_id = item.metadata['source_ref'].split(':', 1)[1]
    assert {'material_id': 'mat', 'material_version_id': 'v1', 'source_version': 1} in repo.get_dependency_set(set_id)
    first_version = repo._one('items', item_id=item.id)['source_version']
    message['snapshot']['request_scope']['mode'] = 'selected'
    change(snapshot=message['snapshot'])
    assert repo.get_item(item.id) is None
    with repo.transaction(): repo.repair_item(item.id)
    assert repo._one('items', item_id=item.id)['source_version'] != first_version


def test_sql_call_ledger_is_independent_and_rejects_business_lock_nesting(store):
    repo, mid, _, _, _ = store
    if not hasattr(repo, 'unit_of_work'): pytest.skip('SQL independent transaction contract')
    from knowpath_backend.learning.agent_memory.repository import SqlAlchemyCallLedger
    ledger = SqlAlchemyCallLedger(repo.unit_of_work, repo.space_id)
    run_id = repo._business_message(mid)['run_id']
    call_id = str(uuid4())
    ledger.reserve(call_id, mid, run_id, 0, 'agent', {'max_tokens': 100})
    with pytest.raises(RuntimeError):
        with repo.transaction():
            ledger.transition(call_id, 'sent')
    assert ledger.get(call_id)['status'] == 'reserved'
    ledger.transition(call_id, 'sent')
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.project_completed(mid)
            raise RuntimeError('business rollback')
    assert ledger.get(call_id)['status'] == 'sent'


def test_original_material_nested_transaction_rolls_back_only_inner_state():
    owner = InMemoryMaterialRepository()
    with owner.transaction():
        owner.learning_spaces['outer'] = {'id': 'outer'}
        with pytest.raises(ValueError):
            with owner.transaction():
                owner.learning_spaces['inner'] = {'id': 'inner'}
                owner.agent_memory_data['spaces']['inner'] = {'space_id': 'inner'}
                raise ValueError('inner rollback')
        assert 'outer' in owner.learning_spaces
        assert 'inner' not in owner.learning_spaces
        assert owner.agent_memory_data['spaces'] == {}


def test_failed_call_restoration_preserves_whether_request_was_sent(store):
    repo, mid, _, _, _ = store
    run_id = repo._business_message(mid)['run_id']
    unsent, sent = str(uuid4()), str(uuid4())
    with repo.transaction():
        repo.reserve_call(unsent, mid, run_id, 0, 'agent', {})
        repo.transition_call(unsent, 'failed')
        repo.reserve_call(sent, mid, run_id, 0, 'agent', {})
        repo.transition_call(sent, 'sent')
        repo.transition_call(sent, 'failed')
    restored = type(repo)(repo.materials if hasattr(repo, 'materials') else repo.unit_of_work, repo.space_id)
    assert restored._one('calls', call_id=unsent)['possibly_sent'] is False
    assert restored._one('calls', call_id=sent)['possibly_sent'] is True


def test_space_delete_cascades_all_eight_stores_and_rolls_back(store):
    from knowpath_backend.learning.persistence.space_repository import InMemorySpaceRepository, SqlAlchemySpaceRepository
    from knowpath_backend.memory.playbook.card import PlaybookCard
    repo, mid, cid, _, _ = store
    other_sid, other_cid, other_mid = str(uuid4()), str(uuid4()), str(uuid4())
    message = repo._business_message(mid)
    message.update(id=other_mid, space_id=other_sid, conversation_id=other_cid)
    message['snapshot']['request_scope']['space_id'] = other_sid
    if hasattr(repo, 'materials'):
        owner = repo.materials
        owner.learning_spaces[other_sid] = {'id': other_sid}
        owner.assessment_data['conversations'][other_cid] = {'id': other_cid, 'space_id': other_sid}
        owner.assessment_data['messages'][other_mid] = message
        peer = type(repo)(owner, other_sid)
        spaces = InMemorySpaceRepository(owner)
        def delete_business():
            owner.assessment_data['messages'].pop(mid)
            owner.assessment_data['conversations'].pop(cid)
    else:
        with repo.transaction(), repo.unit_of_work.session() as session:
            session.add(LearningSpaceRow(id=other_sid, name='other', status='active', bindings=[], topic_ids=[], excluded_topic_ids=[], created_at=message['created_at'], updated_at=message['created_at']))
            session.flush()
            session.add(ConversationRow(id=other_cid, space_id=other_sid, created_at=message['created_at']))
            session.flush()
            session.add(LearningMessageRow(**message))
        peer = type(repo)(repo.unit_of_work, other_sid)
        spaces = SqlAlchemySpaceRepository(repo.unit_of_work)
        def delete_business():
            with repo.unit_of_work.session() as session:
                session.execute(delete(LearningMessageRow).where(LearningMessageRow.id == mid))
                session.execute(delete(ConversationRow).where(ConversationRow.id == cid))
    def seed(target, message_id, conversation_id):
        with target.transaction():
            target.ensure_space(); target.project_completed(message_id)
            target.put_card(PlaybookCard(id=str(uuid4()), content='saved', type='preference', source='user_explicit'))
            target.save_dependency_set('set-' + message_id, message_id, [{'message_id': message_id}])
            target.save_checkpoint(MemoryCheckpoint(space_id=target.space_id, conversation_id=conversation_id, state={'dependency_set_ids': ['set-' + message_id]}))
            target.put_commit('turn:' + message_id, message_id, {'message_id': message_id})
            target.reserve_call(str(uuid4()), message_id, target._business_message(message_id)['run_id'], 0, 'agent', {})
            target.forget_item(target.items_for_turn(message_id)[0].id)
    seed(repo, mid, cid); seed(peer, other_mid, other_cid)
    keys = ('spaces', 'items', 'cards', 'sessions', 'dependencies', 'commits', 'tombstones', 'calls')
    before = {key: repo._rows(key) for key in keys}
    assert all(before.values())
    with pytest.raises(RuntimeError):
        with repo.transaction():
            delete_business(); spaces.delete(repo.space_id)
            assert all(repo._rows(key) == [] for key in keys)
            raise RuntimeError('rollback deletion')
    assert {key: repo._rows(key) for key in keys} == before
    with repo.transaction(): delete_business(); spaces.delete(repo.space_id)
    assert all(repo._rows(key) == [] for key in keys)
    assert all(peer._rows(key) for key in keys)


def test_commit_replay_preserves_original_receipt_revision_after_new_mutation(store):
    repo, _, _, _, _ = store
    with repo.transaction():
        first = repo.put_commit('control:c', {'x': 1}, {'ok': True})
        repo.apply_mutation(MemoryMutation())
        assert repo.get_space()['revision'] == 1
        replay = repo.put_commit('control:c', {'x': 1}, {'ok': True})
        assert replay == first
        assert replay['revision'] == 0
        with pytest.raises(ValueError): repo.put_commit('control:c', {'x': 2}, {'ok': True})
        with pytest.raises(ValueError): repo.put_commit('control:c', {'x': 1}, {'ok': False})
    assert repo.get_commit('control:c') == first


def test_builder_cannot_rebind_old_embedding_result_to_new_model_generation(store):
    from knowpath_backend.learning.agent_memory.repository import InvalidMutation
    repo, mid, _, _, _ = store
    with repo.transaction(): repo.project_completed(mid)
    item = repo.items_for_turn(mid)[0]
    with repo.transaction(): old = repo.index_identity(item.id, model='A', dimension=2)
    captured = repo.candidate_index_identity(mid, item.role, model='A', dimension=2)
    with repo.transaction(): repo.index_identity(item.id, model='B', dimension=2)
    with repo.transaction(): assert not repo.complete_index(old, [1, 0])
    mutation = repo.build_mutation(index_results=[{'identity': captured, 'vector': [1, 0]}])
    assert mutation.operations[0]['result']['identity'] == captured
    with repo.transaction(), pytest.raises(InvalidMutation): repo.apply_mutation(mutation)
    assert repo._one('items', item_id=item.id)['embedding_model'] == 'B'
    assert repo._one('items', item_id=item.id)['vector'] is None


def test_captured_candidate_identity_rejects_revision_and_deletion_changes(store):
    from knowpath_backend.learning.agent_memory.repository import InvalidMutation, item_id_for
    repo, mid, _, _, _ = store
    captured = repo.candidate_index_identity(mid, 'user', model='A', dimension=2)
    with repo.transaction(): repo.apply_mutation(MemoryMutation())
    stale = repo.build_mutation(project_message_ids=[mid], index_results=[{'identity': captured, 'vector': [1, 0]}])
    with repo.transaction(), pytest.raises(InvalidMutation): repo.apply_mutation(stale)
    assert repo._rows('items') == []
    captured = repo.candidate_index_identity(mid, 'user', model='A', dimension=2)
    with repo.transaction(): repo.forget_item(item_id_for(mid, 'user'))
    stale = repo.build_mutation(project_message_ids=[mid], index_results=[{'identity': captured, 'vector': [1, 0]}])
    with repo.transaction(), pytest.raises(InvalidMutation): repo.apply_mutation(stale)
    assert repo._rows('items') == []


def test_new_turn_candidate_identity_can_be_captured_before_completed_write(store):
    repo, mid, _, change, _ = store
    completed = repo._business_message(mid)
    change(status='pending', response=None)
    captured = repo.candidate_index_identity(mid, 'user', model='A', dimension=2, completed_message=completed)
    assert repo._rows('items') == []
    assert repo._business_message(mid)['status'] == 'pending'
    mutation = repo.build_mutation(project_message_ids=[mid], index_results=[{'identity': captured, 'vector': [1, 0]}])
    with repo.transaction():
        change(status='completed', response=completed['response'])
        repo.apply_mutation(mutation)
    assert repo._one('items', item_id=captured['item_id'])['vector'] == [1, 0]


@pytest.mark.parametrize('field,new_value', [('retrieval_version_id', 'retrieval-2'), ('citation_schema_version', 1),
    ('artifact_hash', 'b' * 64), ('start', 1), ('end', 9), ('page', 2), ('block', 'block-2')])
def test_full_citation_and_span_identity_survives_and_invalidates_vectors(store, field, new_value):
    repo, mid, _, change, _ = store
    citation = {'citation_schema_version': 2, 'material_id': 'mat', 'material_version_id': 'v1',
        'retrieval_version_id': 'retrieval-1', 'chunk_id': 'chunk-1', 'source_spans': [
            {'material_version_id': 'v1', 'artifact_hash': 'a' * 64, 'start': 0, 'end': 10, 'page': 1, 'block': 'block-1'}]}
    change(response={'text': 'original answer', 'citations': [citation]})
    with repo.transaction(): repo.project_completed(mid)
    item = repo.items_for_turn(mid)[0]
    with repo.transaction(): identity = repo.index_identity(item.id, model='embed', dimension=2)
    set_id = item.metadata['source_ref'].split(':', 1)[1]
    records = repo.get_dependency_set(set_id)
    assert any(r.get('retrieval_version_id') == 'retrieval-1' for r in records)
    assert any(r.get('artifact_hash') == 'a' * 64 and r.get('start') == 0 and r.get('end') == 10 and r.get('block') == 'block-1' for r in records)
    if field in citation: citation[field] = new_value
    else: citation['source_spans'][0][field] = new_value
    change(response={'text': 'original answer', 'citations': [citation]})
    assert repo.get_item(item.id) is None
    with repo.transaction():
        assert not repo.complete_index(identity, [1, 0])
        repo.repair_item(item.id)
        assert not repo.complete_index(identity, [1, 0])


def test_vector_remove_has_no_side_effect_outside_model_scope_or_predicate(store):
    from knowpath_backend.learning.agent_memory.backends import RepositoryVectorBackend
    repo, mid, _, _, _ = store
    with repo.transaction(): repo.project_completed(mid)
    item = repo.items_for_turn(mid)[0]
    owner = RepositoryVectorBackend(repo, model='B', dimension=2)
    with repo.transaction(): owner.add(item, [1, 0])
    for alien in (RepositoryVectorBackend(repo, model='A', dimension=2),
        RepositoryVectorBackend(repo, model='B', dimension=2, scope_filters={'scope_hash': 'other'}),
        RepositoryVectorBackend(repo, model='B', dimension=2, predicate=lambda _: False)):
        before = repo._one('items', item_id=item.id)
        with repo.transaction(): assert alien.remove(item.id) is None
        assert repo._one('items', item_id=item.id) == before
    with repo.transaction(): assert owner.remove(item.id).id == item.id
    assert owner.items() == []


@pytest.mark.parametrize('damage', ['scalar_snapshot', 'nested_refs', 'scalar_citations', 'incomplete_span'])
def test_corrupt_source_containers_are_invisible_without_read_writes(store, damage):
    repo, mid, _, change, _ = store
    with repo.transaction(): repo.project_completed(mid)
    item = repo.items_for_turn(mid)[0]
    message = repo._business_message(mid)
    if damage == 'scalar_snapshot': change(snapshot='invalid JSON container')
    elif damage == 'nested_refs':
        message['snapshot']['delivered_source_refs'] = [{'material_version_id': 'v1', 'source_spans': ['invalid']}]
        change(snapshot=message['snapshot'])
    elif damage == 'scalar_citations': change(response={'text': 'original answer', 'citations': 'invalid'})
    else: change(response={'text': 'original answer', 'citations': [{'material_version_id': 'v1', 'source_spans': [{'material_version_id': 'v1'}]}]})
    before = repo._rows('items')
    assert repo.get_item(item.id) is None
    assert repo.items_for_turn(mid) == []
    assert repo._rows('items') == before
    with repo.transaction(): assert repo.repair_item(item.id) is None
    assert repo._one('items', item_id=item.id)['lifecycle'] == 'quarantined'


def test_generic_canonical_span_contract_keeps_complete_identity_and_rejects_bad_coordinates():
    from knowpath_backend.memory.dependencies import canonical_dependency
    span = {'material_version_id': 'v', 'retrieval_version_id': 'r', 'artifact_hash': 'a' * 64,
        'start': 0, 'end': 10, 'page': 1, 'block': 'b', 'citation_schema_version': 2}
    assert canonical_dependency(span) == span
    for values in ({'start': -1}, {'end': 0}, {'page': 0}, {'artifact_hash': 'bad'}, {'start': True}, {'block': 123}, {'retrieval_version_id': 123}):
        with pytest.raises(ValueError): canonical_dependency(dict(span, **values))


def test_shared_citation_spans_keep_parent_grouping_in_identity(store):
    repo, mid, _, change, _ = store
    a = {'material_version_id': 'v1', 'artifact_hash': 'a' * 64, 'start': 0, 'end': 10, 'page': 1, 'block': 'a'}
    b = dict(a, start=10, end=20, block='b')
    header = {'citation_schema_version': 2, 'material_id': 'mat', 'material_version_id': 'v1', 'retrieval_version_id': 'r', 'chunk_id': 'c'}
    citations = [dict(header, source_spans=[a]), dict(header, source_spans=[a, b])]
    change(response={'text': 'original answer', 'citations': citations})
    with repo.transaction(): repo.project_completed(mid)
    item = repo.items_for_turn(mid)[0]
    with repo.transaction(): old = repo.index_identity(item.id, model='embed', dimension=2)
    citations[1]['source_spans'] = [b]  # same global parent and span sets; different grouping
    change(response={'text': 'original answer', 'citations': citations})
    assert repo.get_item(item.id) is None
    with repo.transaction(): assert not repo.complete_index(old, [1, 0])


@pytest.mark.parametrize('bad_version', [None, 123, '', 'v2', 'x' * 129])
def test_nested_span_version_must_be_valid_and_match_citation(store, bad_version):
    from knowpath_backend.learning.agent_memory.repository import projection_provenance
    repo, mid, _, change, _ = store
    with repo.transaction(): repo.project_completed(mid)
    item = repo.items_for_turn(mid)[0]
    citation = {'material_id': 'mat', 'material_version_id': 'v1', 'chunk_id': 'retrieved',
        'source_spans': [{'material_version_id': bad_version, 'artifact_hash': 'a' * 64,
                          'start': 0, 'end': 10, 'page': 1, 'block': 'original'}]}
    change(response={'text': 'original answer', 'citations': [citation]})
    with pytest.raises(ValueError): projection_provenance(repo._business_message(mid))
    before = repo._rows('items')
    assert repo.get_item(item.id) is None
    assert repo.items_for_turn(mid) == []
    assert repo._rows('items') == before
    with repo.transaction(), pytest.raises(ValueError): repo.project_completed(mid)
    assert repo._rows('items') == before
    with repo.transaction(): assert repo.repair_item(item.id) is None
    assert repo._one('items', item_id=item.id)['lifecycle'] == 'quarantined'


@pytest.mark.parametrize('commit', [True, False])
@pytest.mark.parametrize('read_kind', ['public', 'raw', 'business'])
def test_memory_readers_wait_for_transaction_and_observe_only_committed_snapshot(store, commit, read_kind):
    repo, mid, cid, _, _ = store
    if not hasattr(repo, 'materials'): pytest.skip('in-memory reader lock contract')
    from knowpath_backend.memory.playbook.card import PlaybookCard
    new_mid = str(uuid4())
    item_id = None
    card = PlaybookCard(id='isolation-card', content='old card', type='preference', source='user_explicit')
    with repo.transaction():
        repo.project_completed(mid)
        repo.put_card(card)
        repo.save_checkpoint(MemoryCheckpoint(space_id=repo.space_id, conversation_id=cid, state={'marker': 'old'}))
    item_id = repo.items_for_turn(mid)[0].id
    old_containers = repo.materials.agent_memory_data
    modified, release, reader_entered, reader_finished = Event(), Event(), Event(), Event()
    def snapshot():
        return {'items': sorted((i.id, i.access_count) for i in repo.list_items()),
            'card': repo.get_card(card.id).content, 'checkpoint': repo.get_checkpoint(cid, 0).state,
            'raw': repo._rows('items'), 'business': repo._business_message(new_mid)}
    before = snapshot()
    def writer():
        with repo.transaction():
            message = repo._business_message(mid)
            message.update(id=new_mid, sequence=2)
            repo.materials.assessment_data['messages'][new_mid] = message
            repo.project_completed(new_mid)
            item = repo.get_item(item_id); item.touch(); repo.update_item(item)
            changed_card = card.model_copy(update={'content': 'new card'})
            repo.put_card(changed_card)
            repo.save_checkpoint(MemoryCheckpoint(space_id=repo.space_id, conversation_id=cid, state={'marker': 'new'}))
            modified.set()
            assert release.wait(timeout=5), 'test must release writer'
            if not commit: raise RuntimeError('rollback writer')
    def reader():
        assert not repo.active
        reader_entered.set()
        try:
            if read_kind == 'raw': return repo._rows('items')
            if read_kind == 'business': return repo._business_message(new_mid)
            return snapshot()
        finally: reader_finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        write_future = pool.submit(writer)
        assert modified.wait(timeout=3)
        read_future = pool.submit(reader)
        try:
            assert reader_entered.wait(timeout=3)
            assert not reader_finished.wait(timeout=0.1), 'reader leaked uncommitted state'
        finally: release.set()
        if commit: write_future.result(timeout=3)
        else:
            with pytest.raises(RuntimeError, match='rollback writer'): write_future.result(timeout=3)
        observed = read_future.result(timeout=3)
    after = snapshot()
    expected = after if commit else before
    assert observed == (expected['raw'] if read_kind == 'raw' else expected['business'] if read_kind == 'business' else expected)
    if commit:
        assert len(after['items']) == 4
        assert dict(after['items'])[item_id] == 1
        assert after['card'] == 'new card' and after['checkpoint'] == {'marker': 'new'}
    else:
        assert after == before
        assert repo.materials.agent_memory_data is not old_containers


@pytest.mark.parametrize('bad_version', [None, 123, '', 'x' * 129])
def test_flat_source_dependency_span_requires_valid_material_version(store, bad_version):
    from knowpath_backend.memory.dependencies import canonical_dependency
    repo, mid, _, change, _ = store
    with repo.transaction(): repo.project_completed(mid)
    item = repo.items_for_turn(mid)[0]
    span = {'material_version_id': bad_version, 'artifact_hash': 'a' * 64, 'start': 0, 'end': 1}
    with pytest.raises(ValueError): canonical_dependency(span)
    change(response={'text': 'original answer', 'source_dependencies': [span]})
    before = repo._rows('items')
    assert repo.get_item(item.id) is None
    assert repo.items_for_turn(mid) == []
    assert repo._rows('items') == before
    with repo.transaction(), pytest.raises(ValueError): repo.project_completed(mid)
    with repo.transaction(): assert repo.repair_item(item.id) is None
    assert repo._one('items', item_id=item.id)['lifecycle'] == 'quarantined'
