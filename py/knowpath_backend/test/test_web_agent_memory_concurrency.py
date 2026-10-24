"""Lease fencing uses real memory/SQL storage, without provider calls."""
from datetime import timedelta
import pytest
from knowpath_backend.test.test_web_agent_memory_repository import store


def test_execution_lease_exclusive_unique_and_conditionally_released(store):
    repo, mid, _, _, _ = store
    with repo.transaction():
        first = repo.acquire_execution_lease(mid, 'worker-a', seconds=30)
    assert first is not None
    with repo.transaction():
        assert repo.acquire_execution_lease(mid, 'worker-b', seconds=30) is None
        assert not repo.release_execution_lease('stale-token', mid, 'worker-a')
        assert repo.renew_execution_lease(first['lease_token'], mid, 'worker-a', seconds=30)
        assert repo.release_execution_lease(first['lease_token'], mid, 'worker-a')
        second = repo.acquire_execution_lease(mid, 'worker-a', seconds=30)
        assert second['lease_token'] != first['lease_token']
        assert not repo.release_execution_lease(first['lease_token'], mid, 'worker-a')
    assert repo.get_space()['lease_token'] == second['lease_token']

def test_expired_token_cannot_publish_before_or_after_takeover(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService, MemoryLease
    from knowpath_backend.learning.workers.model_tasks import LeaseLost
    repo,mid,cid,_,_=store
    old=MemoryLease(repo,mid,'old');assert old.acquire()
    session=MemorySessionService(repo).load(cid,0,'scope',eligible=lambda item,purpose:True)
    mutation=session.freeze(project_message_ids=[mid])
    with repo.transaction():
        row=repo._locked_space();row['lease_expires']=repo.execution_time()-timedelta(seconds=1);repo._put('spaces',row)
    with repo.transaction(),pytest.raises(LeaseLost):session.publish(mid,old,mutation)
    replacement=MemoryLease(repo,mid,'new');assert replacement.acquire()
    with repo.transaction(),pytest.raises(LeaseLost):session.publish(mid,old,mutation)
    assert repo.get_commit('turn:'+mid) is None
    assert repo.items_for_turn(mid)==[]
    assert repo.get_checkpoint(cid,0) is None
    assert repo.get_space()['lease_token']==replacement.token
    session.close();replacement.release()


def test_expired_execution_token_cannot_renew_or_release_new_owner(store):
    repo, mid, _, _, _ = store
    with repo.transaction():
        first = repo.acquire_execution_lease(mid, 'worker-a', seconds=30)
        state = repo._locked_space()
        state['lease_expires'] = repo.execution_time() - timedelta(seconds=1)
        repo._put('spaces', state)
        assert not repo.renew_execution_lease(first['lease_token'], mid, 'worker-a', seconds=30)
        second = repo.acquire_execution_lease(mid, 'worker-b', seconds=30)
        assert second['lease_token'] != first['lease_token']
        assert not repo.renew_execution_lease(first['lease_token'], mid, 'worker-a', seconds=30)
        assert not repo.release_execution_lease(first['lease_token'], mid, 'worker-a')
    assert repo.get_space()['lease_token'] == second['lease_token']

def test_lease_rejects_missing_message_and_writes_outside_transaction(store):
    repo, mid, _, _, _ = store
    with pytest.raises(RuntimeError, match='active'):
        repo.acquire_execution_lease(mid, 'worker')
    with repo.transaction(), pytest.raises(ValueError, match='message'):
        repo.acquire_execution_lease('missing', 'worker')

def test_lease_rejects_message_from_another_space(store):
    repo, mid, _, change, _ = store
    from uuid import uuid4
    from knowpath_backend.learning.agent_memory.repository import InMemoryMemoryRepository, SqlAlchemyMemoryRepository
    from knowpath_backend.learning.persistence.db import LearningSpaceRow
    from datetime import datetime, timezone
    sid = str(uuid4())
    if hasattr(repo, 'materials'):
        repo.materials.learning_spaces[sid] = {'id': sid}
        other = InMemoryMemoryRepository(repo.materials, sid)
    else:
        with repo.transaction(), repo.unit_of_work.session() as session:
            instant = datetime.now(timezone.utc)
            session.add(LearningSpaceRow(id=sid, name='other', status='active', bindings=[], topic_ids=[], excluded_topic_ids=[], created_at=instant, updated_at=instant))
        other = SqlAlchemyMemoryRepository(repo.unit_of_work, sid)
    with other.transaction(): other.ensure_space()
    with other.transaction(), pytest.raises(ValueError, match='message'):
        other.acquire_execution_lease(mid, 'worker')

from knowpath_backend.test.test_learning_state_persistence import workspace

def test_mysql_expired_executor_cannot_publish_after_replacement(workspace):
    from knowpath_backend.learning.persistence.unit_of_work import SqlAlchemyUnitOfWork
    from knowpath_backend.learning.agent_memory.repository import SqlAlchemyMemoryRepository
    from knowpath_backend.learning.agent_memory.service import MemorySessionService, MemoryLease
    from knowpath_backend.learning.workers.model_tasks import LeaseLost
    factory,sid,_,engine=workspace
    if engine is None or engine.dialect.name!='mysql':pytest.skip('requires dedicated MySQL connections')
    state=factory();state.send_message(sid,{'message':'Explain functions'},dispatch=lambda *args:None)
    message=state.message_service.repository.records('messages',space_id=sid)[0]
    a=SqlAlchemyMemoryRepository(SqlAlchemyUnitOfWork(engine),sid)
    b=SqlAlchemyMemoryRepository(SqlAlchemyUnitOfWork(engine),sid)
    lease=MemoryLease(a,message['id'],'old');assert lease.acquire()
    session=MemorySessionService(a).load(message['conversation_id'],0,'scope',eligible=lambda item,purpose:True)
    mutation=session.freeze(project_message_ids=[message['id']])
    with b.transaction():
        row=b._locked_space();row['lease_expires']=b.execution_time()-timedelta(seconds=1);b._put('spaces',row)
    with a.transaction(),pytest.raises(LeaseLost):session.publish(message['id'],lease,mutation)
    replacement=MemoryLease(b,message['id'],'new');assert replacement.acquire()
    with a.transaction(),pytest.raises(LeaseLost):session.publish(message['id'],lease,mutation)
    assert a.get_commit('turn:'+message['id']) is None
    assert a.items_for_turn(message['id'])==[]
    assert a.get_checkpoint(message['conversation_id'],0) is None
    assert b.get_space()['lease_token']==replacement.token
    session.close();replacement.release()

def test_mysql_two_connections_fence_expired_token_and_single_winner(workspace):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from knowpath_backend.learning.persistence.unit_of_work import SqlAlchemyUnitOfWork
    from knowpath_backend.learning.agent_memory.repository import SqlAlchemyMemoryRepository
    factory,sid,_,engine=workspace
    if engine is None or engine.dialect.name!='mysql': pytest.skip('requires dedicated MySQL double connection')
    state=factory()
    state.send_message(sid,{'message':'Explain functions'},dispatch=lambda *args:None)
    mid=state.message_service.repository.records('messages',space_id=sid)[0]['id']
    barrier=Barrier(2)
    def contender(owner):
        repo=SqlAlchemyMemoryRepository(SqlAlchemyUnitOfWork(engine),sid)
        barrier.wait(timeout=5)
        with repo.transaction():return repo.acquire_execution_lease(mid,owner,seconds=30)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(contender,owner) for owner in ('one','two')]
        results=[future.result(timeout=10) for future in futures]
    assert sum(result is not None for result in results)==1
    first=next(result for result in results if result)
    a=SqlAlchemyMemoryRepository(SqlAlchemyUnitOfWork(engine),sid)
    b=SqlAlchemyMemoryRepository(SqlAlchemyUnitOfWork(engine),sid)
    with a.transaction():
        row=a._locked_space();row['lease_expires']=a.execution_time()-timedelta(seconds=1);a._put('spaces',row)
    with b.transaction():
        assert not b.renew_execution_lease(first['lease_token'],mid,first['owner'])
        second=b.acquire_execution_lease(mid,'replacement')
    with a.transaction():
        assert not a.renew_execution_lease(first['lease_token'],mid,first['owner'])
        assert not a.release_execution_lease(first['lease_token'],mid,first['owner'])
        assert a.check_execution_lease(second['lease_token'],mid,'replacement')
