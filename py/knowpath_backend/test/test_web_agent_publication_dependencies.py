"""Final source qualification fences detached Agent results."""
import pytest
from knowpath_backend.test.test_learning_state_persistence import workspace
from knowpath_backend.test.test_web_agent_memory_repository import store


def test_default_web_maintenance_expires_l3_across_persisted_turns(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    repo,mid,cid,_,_=store
    with repo.transaction():repo.project_completed(mid)
    session=MemorySessionService(repo).load(cid,0,'scope',eligible=lambda value,purpose:True)
    item=session.manager.cold.items()[0]
    session.manager.recall_buffer.record_hit(item.id,.2,0)
    ttl=session.manager.config.l3_ttl_turns
    with repo.transaction():repo.apply_mutation(session.freeze())
    session.close()
    for turn in range(1,ttl+2):
        session=MemorySessionService(repo).load(cid,0,'scope',eligible=lambda value,purpose:True)
        session.manager.tick()
        with repo.transaction():repo.apply_mutation(session.freeze())
        session.close()
    final=repo.get_checkpoint(cid,0)
    assert final.state['l3']==[]


@pytest.mark.parametrize('kind', ['message', 'item', 'card'])
def test_consumed_recall_message_invalidated_after_execution_cannot_publish(workspace, monkeypatch, kind):
    factory, sid, _, _ = workspace
    state = factory()
    state.send_message(sid, {'message': 'Explain functions'})
    service = state.message_service
    repo = service.repository
    previous = repo.records('messages', space_id=sid)[0]
    state.send_message(sid, {'message': 'Explain functions again'}, dispatch=lambda *args: None)
    current = next(row for row in repo.records('messages', space_id=sid) if row['id'] != previous['id'])
    memory = repo.agent_memory_factory(sid)
    item = memory.items_for_turn(previous['id'])[0]
    dependency = {'message_id': previous['id']} if kind == 'message' else {'item_id': item.id}
    if kind == 'card':
        from knowpath_backend.memory.playbook.card import PlaybookCard
        card = PlaybookCard(content='Consumed card', type='state', source='user_explicit', metadata=item.metadata)
        with memory.transaction():memory.put_card(card)
        dependency = {'kind': 'card', 'id': card.id}
    before = memory.get_space()['revision']
    checkpoint_before = memory.get_checkpoint(current['conversation_id'], current['snapshot']['memory_scope']['context_epoch'])
    publish = service._publish_answer
    def with_recall(initial, identifier, snapshot, *args, **kwargs):
        snapshot = dict(snapshot, source_dependencies=[dependency])
        return publish(initial, identifier, snapshot, *args, **kwargs)
    monkeypatch.setattr(service, '_publish_answer', with_recall)
    execute = service.commands._execute
    def invalidate(op, *args, **kwargs):
        if op == 'message.publish':
            if kind == 'card':
                with memory.transaction():memory.delete_card(card.id)
            with repo.transaction():
                source = repo.get_record('messages', previous['id'])
                source['status'] = 'failed'
                repo.put_record('messages', source)
        return execute(op, *args, **kwargs)
    monkeypatch.setattr(service.commands, '_execute', invalidate)
    service.generate(current['id'])
    result = repo.get_record('messages', current['id'])
    assert result['status'] == 'failed'
    assert result['response'] is None
    assert memory.get_commit('turn:' + current['id']) is None
    assert memory.items_for_turn(current['id']) == []
    assert memory.get_space()['revision'] == before
    assert memory.get_checkpoint(current['conversation_id'], current['snapshot']['memory_scope']['context_epoch']) == checkpoint_before
    assert not any(e['event'] in {'message.completed', 'run.completed'} for e in state.events_for(current['run_id']))


def test_independent_connection_invalidated_recall_cannot_publish(workspace, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    factory, sid, _, engine = workspace
    if engine is None or engine.dialect.name != 'mysql':
        pytest.skip('requires dedicated MySQL independent connections')
    state = factory(); service = state.message_service; repo = service.repository
    state.send_message(sid, {'message': 'Explain functions'})
    previous = repo.records('messages', space_id=sid)[0]
    state.send_message(sid, {'message': 'Explain functions again'}, dispatch=lambda *args: None)
    current = next(row for row in repo.records('messages', space_id=sid) if row['id'] != previous['id'])
    memory = repo.agent_memory_factory(sid)
    revision = memory.get_space()['revision']
    publish = service._publish_answer
    def add_consumed(initial, identifier, snapshot, *args, **kwargs):
        return publish(initial, identifier, dict(snapshot, source_dependencies=[{'message_id': previous['id']}]), *args, **kwargs)
    monkeypatch.setattr(service, '_publish_answer', add_consumed)
    execute = service.commands._execute
    def invalidate():
        independent = factory().message_service.repository
        with independent.transaction():
            row = independent.get_record('messages', previous['id'])
            row['status'] = 'failed'; independent.put_record('messages', row)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def before_publish(op, *args, **kwargs):
            if op == 'message.publish':pool.submit(invalidate).result(timeout=10)
            return execute(op, *args, **kwargs)
        monkeypatch.setattr(service.commands, '_execute', before_publish)
        service.generate(current['id'])
    assert repo.get_record('messages', current['id'])['status'] == 'failed'
    assert memory.get_space()['revision'] == revision
    assert memory.get_commit('turn:' + current['id']) is None
    assert memory.items_for_turn(current['id']) == []
    assert not any(e['event'] in {'message.completed', 'run.completed'} for e in state.events_for(current['run_id']))


def test_mysql_card_revoked_after_final_qualification_rolls_back_publication(workspace, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from knowpath_backend.learning.agent_memory.service import MemorySession
    from knowpath_backend.learning.workers.model_tasks import LeaseLost
    from knowpath_backend.memory.playbook.card import PlaybookCard
    factory, sid, _, engine = workspace
    if engine is None or engine.dialect.name != 'mysql':pytest.skip('requires dedicated MySQL connections')
    state=factory();service=state.message_service;repo=service.repository
    state.send_message(sid,{'message':'Explain functions'})
    previous=repo.records('messages',space_id=sid)[0]
    memory=repo.agent_memory_factory(sid);source=memory.items_for_turn(previous['id'])[0]
    card=PlaybookCard(content='Consumed card',type='state',source='user_explicit',metadata=source.metadata)
    with memory.transaction():memory.put_card(card)
    state.send_message(sid,{'message':'Explain functions again'},dispatch=lambda *args:None)
    current=next(row for row in repo.records('messages',space_id=sid) if row['id']!=previous['id'])
    revision=memory.get_space()['revision']
    publish=service._publish_answer
    def add_consumed(initial,identifier,snapshot,*args,**kwargs):
        return publish(initial,identifier,dict(snapshot,source_dependencies=[{'kind':'card','id':card.id}]),*args,**kwargs)
    monkeypatch.setattr(service,'_publish_answer',add_consumed)
    original=MemorySession.publish
    def revoke():
        independent=factory().message_service.repository.agent_memory_factory(sid)
        with independent.transaction():independent.delete_card(card.id)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def after_qualification(session,*args,**kwargs):
            pool.submit(revoke).result(timeout=10)
            return original(session,*args,**kwargs)
        monkeypatch.setattr(MemorySession,'publish',after_qualification)
        with pytest.raises(LeaseLost):service.generate(current['id'])
    assert memory.get_card(card.id) is None
    assert repo.get_record('messages',current['id'])['response'] is None
    assert memory.get_commit('turn:'+current['id']) is None
    assert memory.items_for_turn(current['id'])==[]
    assert memory.get_space()['revision']==revision
    assert not any(e['event'] in {'message.completed','run.completed'} for e in state.events_for(current['run_id']))


def test_publication_locks_sources_and_all_messages_before_run_and_memory(workspace, monkeypatch):
    from knowpath_backend.learning.agent_memory.repository import MemoryRepository
    factory,sid,_,engine=workspace
    if engine is None:pytest.skip('in-memory store has no SQL row locks')
    state=factory();service=state.message_service;repo=service.repository
    state.send_message(sid,{'message':'Explain functions'})
    previous=repo.records('messages',space_id=sid)[0]
    state.send_message(sid,{'message':'Explain functions again'},dispatch=lambda *args:None)
    current=next(row for row in repo.records('messages',space_id=sid) if row['id']!=previous['id'])
    trace=[];inside=[False]
    once=service._publish_answer_once
    def recording(*args,**kwargs):
        inside[0]=True
        try:return once(*args,**kwargs)
        finally:inside[0]=False
    monkeypatch.setattr(service,'_publish_answer_once',recording)
    for name in ('get_material','get_version'):
        original=getattr(state.material_repository,name)
        def read(identifier,_original=original,_name=name):
            if inside[0]:trace.append((_name,identifier))
            return _original(identifier)
        monkeypatch.setattr(state.material_repository,name,read)
    get=repo.get_record
    def record(table,identifier,*args,**kwargs):
        if inside[0] and table=='messages' and kwargs.get('lock',True):trace.append(('message',identifier))
        return get(table,identifier,*args,**kwargs)
    monkeypatch.setattr(repo,'get_record',record)
    run_get=service.runs.get
    def run(identifier):
        if inside[0]:trace.append(('run',identifier))
        return run_get(identifier)
    monkeypatch.setattr(service.runs,'get',run)
    locked=MemoryRepository._locked_space
    def memory_lock(memory):
        if inside[0]:trace.append(('memory',memory.space_id))
        return locked(memory)
    monkeypatch.setattr(MemoryRepository,'_locked_space',memory_lock)
    publish=service._publish_answer
    def consumed(initial,identifier,snapshot,*args,**kwargs):
        return publish(initial,identifier,dict(snapshot,source_dependencies=[{'message_id':previous['id']}]),*args,**kwargs)
    monkeypatch.setattr(service,'_publish_answer',consumed)
    service.generate(current['id'])
    assert repo.get_record('messages',current['id'])['status']=='completed'
    first_message=next(i for i,event in enumerate(trace) if event[0]=='message')
    first_run=next(i for i,event in enumerate(trace) if event[0]=='run')
    first_memory=next(i for i,event in enumerate(trace) if event[0]=='memory')
    assert any(event[0]=='get_material' for event in trace[:first_message])
    assert any(event[0]=='get_version' for event in trace[:first_message])
    assert {identifier for kind,identifier in trace[:first_run] if kind=='message'} >= {previous['id'],current['id']}
    assert first_message < first_run < first_memory


def test_memory_preparation_capacity_failure_is_terminal_and_publishes_nothing(workspace, monkeypatch):
    from knowpath_backend.learning.agent_memory.service import MemorySession
    from knowpath_backend.memory.checkpoint import InvalidCheckpoint
    factory,sid,_,_=workspace
    state=factory();service=state.message_service
    result=state.send_message(sid,{'message':'Explain functions'},dispatch=lambda *args:None)
    current=service.repository.records('messages',space_id=sid)[0]
    def capacity(*args,**kwargs):raise InvalidCheckpoint('Derived dependency capacity exceeded')
    monkeypatch.setattr(MemorySession,'freeze',capacity)
    service.generate(current['id'])
    final=service.repository.get_record('messages',current['id'])
    assert final['status']=='failed' and final['response'] is None
    memory=service.repository.agent_memory_factory(sid)
    assert memory.get_commit('turn:'+current['id']) is None
    assert memory.items_for_turn(current['id'])==[]
    assert memory.get_checkpoint(current['conversation_id'],current['snapshot']['memory_scope']['context_epoch']) is None
    assert not any(e['event'] in {'message.completed','run.completed'} for e in state.events_for(result['run_id']))
