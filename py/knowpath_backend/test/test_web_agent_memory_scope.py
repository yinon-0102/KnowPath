"""Scope qualification contract; all checks run without providers or daily DB."""
from copy import deepcopy
import importlib

import pytest


def api():
    try:
        return importlib.import_module('knowpath_backend.learning.agent_memory.scope')
    except ModuleNotFoundError:
        pytest.fail('Task4 shared scope policy is not implemented')


def identity(*, space='s', ids=None, mode='selected', answer='material', entry='chat', reading=None, lesson=None):
    bindings = [dict(material_id=m, material_version_id=m + '-v1', graph_version=1) for m in ('a', 'b')]
    selected = ['a'] if ids is None else ids
    return api().normalize_scope(dict(id=space, scope_version=1, bindings=bindings),
        dict(space_id=space, scope_version=1, bindings=[b for b in bindings if b['material_id'] in selected],
             mode=mode, material_ids=selected), entry_type=entry, reading_context=reading,
        lesson_fingerprint=lesson, answer_mode=answer)


def frozen(scope=None, *, epoch=1, conversation='c'):
    scope = scope or identity()
    return dict(space_id=scope['space_id'], conversation_id=conversation, context_epoch=epoch,
                receive_sequence=epoch, fingerprint=api().scope_fingerprint(scope), identity=scope)


def candidate(scope=None, **changes):
    value = dict(id='x', metadata=dict(memory_scope=frozen(scope),
        source_dependencies=[dict(material_id='a', material_version_id='a-v1')]))
    value.update(changes)
    return value


def policy(current=None, **kwargs):
    return api().EligibilityPolicy(current or frozen(), dependency_resolver=lambda dep: True,
                                    tombstone_checker=lambda kind, value: False, **kwargs)


def test_normalization_sorts_deduplicates_but_preserves_all_mode():
    assert identity(ids=['b', 'a', 'a']) == identity(ids=['a', 'b'])
    assert api().scope_fingerprint(identity(ids=['a', 'b'], mode='all')) != api().scope_fingerprint(identity(ids=['a', 'b']))
    assert api().scope_fingerprint(identity(answer='auto')) != api().scope_fingerprint(identity(answer='material'))


@pytest.mark.parametrize('other', [dict(space='elsewhere'), dict(ids=['b']), dict(answer='general'),
    dict(entry='reading', reading={'material_id': 'a', 'material_version_id': 'a-v1'}),
    dict(entry='lesson', lesson='lesson-v1')])
def test_scope_switch_blocks_all_read_paths(other):
    value = candidate(identity(**other))
    for purpose in ('L0', 'L1', 'L2', 'L3', 'L4', 'L5', 'recall', 'hook', 'kg'):
        assert not policy().eligible(value, purpose=purpose)


def test_returned_a_is_recall_only_not_adjacent_history():
    old = candidate()
    current = frozen(epoch=3)
    for layer in ('L1', 'L2', 'L3'):
        assert not policy(current).eligible(old, purpose=layer)
    assert policy(current).eligible(old, purpose='recall')
    assert policy(current).eligible(old, purpose='L0')


def test_preference_requires_proof_and_never_crosses_space():
    value = candidate(identity(ids=['b']))
    value['metadata'].update(memory_kind='preference', source_independent=True, source_dependencies=[])
    assert not policy().eligible(value, purpose='L0')  # model-writable booleans prove nothing
    trusted = policy(preference_verifier=lambda value: True)
    assert trusted.eligible(value, purpose='L0')
    value['metadata']['memory_scope'] = frozen(identity(space='other'))
    assert not trusted.eligible(value, purpose='L0')
    value['metadata']['memory_scope'] = frozen(identity(ids=['b']))
    del value['metadata']['source_independent']
    assert not trusted.eligible(value, purpose='L0')


def test_pinning_never_exempts_missing_source_and_tombstone_wins():
    value = candidate(pinned=True)
    denied = api().EligibilityPolicy(frozen(), dependency_resolver=lambda dep: False,
                                     tombstone_checker=lambda kind, value: False)
    assert not denied.eligible(value, purpose='L0')
    denied = api().EligibilityPolicy(frozen(), dependency_resolver=lambda dep: True,
                                     tombstone_checker=lambda kind, value: True)
    assert not denied.eligible(value, purpose='recall')


def test_complete_external_dependencies_checked_before_ranking():
    value = candidate()
    value['metadata']['source_ref'] = 'dependencies:full'
    calls = []
    gate = api().EligibilityPolicy(frozen(), dependency_resolver=lambda dep: dep.get('chunk_id') != 'deleted',
        tombstone_checker=lambda kind, value: False,
        dependency_sets=lambda key: [*value['metadata']['source_dependencies'], dict(material_id='a', material_version_id='a-v1', chunk_id='deleted')])
    assert gate.rank([value], purpose='hook', key=lambda v: calls.append(v) or 1) == []
    assert calls == []
    gate = api().EligibilityPolicy(frozen(), dependency_resolver=lambda dep: True,
        tombstone_checker=lambda kind, value: False, dependency_sets=lambda key: None)
    assert not gate.eligible(value, purpose='L4')


def test_dependency_read_failure_is_denied_not_empty():
    def unavailable(*args): raise RuntimeError('unavailable')
    gate = api().EligibilityPolicy(frozen(), dependency_resolver=unavailable, tombstone_checker=lambda *args: False)
    assert not gate.eligible(candidate(), purpose='kg')


def test_derived_cards_inherit_dependencies_and_unknown_is_source_bound():
    scope = api()
    parent = candidate()
    inherited = scope.inherit_dependencies([parent])
    assert inherited['memory_kind'] == 'source'
    assert inherited['source_dependencies'] == parent['metadata']['source_dependencies']
    assert scope.inherit_dependencies([])['source_independent'] is False


@pytest.fixture(params=['memory', 'sqlite'])
def reception_store(request, tmp_path):
    from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
    from knowpath_backend.learning.agent_memory.repository import InMemoryMemoryRepository, SqlAlchemyMemoryRepository
    from knowpath_backend.learning.persistence.db import Base, LearningSpaceRow, ConversationRow, LearningMessageRow
    from knowpath_backend.learning.persistence.unit_of_work import SqlAlchemyUnitOfWork
    from sqlalchemy import create_engine
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if request.param == 'memory':
        owner = InMemoryMaterialRepository()
        owner.learning_spaces['s'] = {'id': 's'}
        owner.assessment_data['conversations']['c'] = dict(id='c', space_id='s')
        owner.assessment_data['conversations']['d'] = dict(id='d', space_id='s')
        repo = InMemoryMemoryRepository(owner, 's')
        def add(mid, conversation='c'):
            owner.assessment_data['messages'][mid] = dict(id=mid, space_id='s', conversation_id=conversation,
                status='pending', snapshot={}, sequence=len(owner.assessment_data['messages']) + 1)
        def status(mid, value): owner.assessment_data['messages'][mid]['status'] = value
    else:
        engine = create_engine('sqlite+pysqlite:///' + str(tmp_path / 'reception.db'))
        Base.metadata.create_all(engine)
        uow = SqlAlchemyUnitOfWork(engine)
        with uow.transaction(), uow.session() as session:
            session.add(LearningSpaceRow(id='s', name='s', status='active', bindings=[], topic_ids=[], excluded_topic_ids=[], created_at=now, updated_at=now))
            session.flush()
            session.add_all([ConversationRow(id=c, space_id='s', created_at=now) for c in ('c', 'd')])
        repo = SqlAlchemyMemoryRepository(uow, 's')
        def add(mid, conversation='c'):
            with uow.session() as session:
                session.add(LearningMessageRow(id=mid, space_id='s', conversation_id=conversation,
                    run_id='run-' + mid, message='q', status='pending', snapshot={}, sequence=int(mid[1:]), created_at=now))
                session.flush()
        def status(mid, value):
            with uow.session() as session: session.get(LearningMessageRow, mid).status = value
    with repo.transaction(): repo.ensure_space()
    return repo, add, status


def test_pending_receptions_aba_idempotency_and_space_order(reception_store):
    repo, add, status = reception_store
    with repo.transaction():
        add('m1'); a = api().freeze_reception(repo, 'm1', identity())
        add('m2'); b = api().freeze_reception(repo, 'm2', identity(ids=['b']))
        status('m2', 'cancelled')
        add('m3'); again = api().freeze_reception(repo, 'm3', identity())
        add('m4', 'd'); other = api().freeze_reception(repo, 'm4', identity())
    assert [a['context_epoch'], b['context_epoch'], again['context_epoch'], other['context_epoch']] == [1, 2, 3, 1]
    assert [a['receive_sequence'], b['receive_sequence'], again['receive_sequence'], other['receive_sequence']] == [1, 2, 3, 4]
    with repo.transaction(): assert api().freeze_reception(repo, 'm1', identity()) == a
    assert repo.get_space()['receive_sequence'] == 4
    with repo.transaction(), pytest.raises(ValueError): api().freeze_reception(repo, 'm1', identity(ids=['b']))


def test_reception_rollback_leaves_no_segment_and_requires_transaction(reception_store):
    repo, add, _ = reception_store
    with pytest.raises(RuntimeError): api().freeze_reception(repo, 'missing', identity())
    with pytest.raises(RuntimeError, match='abort'):
        with repo.transaction():
            add('m1'); api().freeze_reception(repo, 'm1', identity())
            raise RuntimeError('abort')
    with repo.transaction():
        add('m2'); received = api().freeze_reception(repo, 'm2', identity())
    assert received['context_epoch'] == received['receive_sequence'] == 1

def test_scope_receipt_internal_space_must_match_identity():
    value=candidate()
    value['metadata']['memory_scope']['identity']['space_id']='foreign'
    value['metadata']['memory_scope']['fingerprint']=api().scope_fingerprint(value['metadata']['memory_scope']['identity'])
    current=frozen(value['metadata']['memory_scope']['identity'])
    current['space_id']='s'
    assert not policy(current).eligible(value,purpose='recall')
