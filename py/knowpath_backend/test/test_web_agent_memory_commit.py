"""Request memory copies use the real generic manager and durable repositories."""
import pytest
from knowpath_backend.test.test_web_agent_memory_repository import store


def test_candidate_crash_discards_real_manager_changes(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    from knowpath_backend.memory.manager import MemoryManager
    repo, mid, cid, _, _ = store
    with repo.transaction(): repo.project_completed(mid)
    before = repo._rows('items')
    session = MemorySessionService(repo).load(cid, 0, 'scope', eligible=lambda item, purpose: True)
    assert isinstance(session.manager, MemoryManager)
    assert len(session.manager.cold.items()) == 2
    session.manager.cold.items()[0].touch()
    session.manager.write('candidate question', metadata={'turn_id': 'candidate'})
    session.close()
    assert repo._rows('items') == before
    assert repo.get_checkpoint(cid, 0) is None


def test_frozen_mutation_preserves_captured_revision_and_real_checkpoint(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    from knowpath_backend.learning.agent_memory.repository import InvalidMutation
    from knowpath_backend.memory.contracts import MemoryMutation
    repo, mid, cid, _, _ = store
    with repo.transaction(): repo.project_completed(mid)
    session = MemorySessionService(repo).load(cid, 0, 'scope', eligible=lambda item, purpose: True)
    session.manager.cold.items()[0].touch()
    mutation = session.freeze()
    assert mutation.checkpoint.space_id == repo.space_id
    assert mutation.checkpoint.state['whole_turns'] is True
    assert mutation.expected_revision == 0
    with repo.transaction(): repo.apply_mutation(MemoryMutation())
    with repo.transaction(), pytest.raises(InvalidMutation): repo.apply_mutation(mutation)
    assert repo.get_checkpoint(cid, 0) is None
    session.close()

def test_restart_restores_heat_l3_and_shared_hot_identity(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    repo, mid, cid, _, _ = store
    with repo.transaction(): repo.project_completed(mid)
    service = MemorySessionService(repo)
    first = service.load(cid, 0, 'scope', eligible=lambda item, purpose: True)
    item = first.manager.cold.items()[0]
    first.manager.working.add(item)
    item.touch()
    first.manager.recall_buffer.record_hit(item.id, .8, 0)
    with repo.transaction(): repo.apply_mutation(first.freeze())
    first.close()
    restarted_repo = type(repo)(repo.materials if hasattr(repo, 'materials') else repo.unit_of_work, repo.space_id)
    second = MemorySessionService(restarted_repo).load(cid, 0, 'scope', eligible=lambda item, purpose: True)
    live = second.manager.working.items()[0]
    assert live.access_count == 1
    assert second.manager.cold.get(live.id) is live
    assert second.manager.semantic.get(live.id) is live
    assert second.manager.recall_buffer.entries()[0].hit_count == 1
    second.close()

def test_restored_vector_backend_and_cold_use_same_canonical_instance(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    repo, mid, cid, _, _ = store
    with repo.transaction(): repo.project_completed(mid)
    service = MemorySessionService(repo)
    first = service.load(cid, 0, 'scope', eligible=lambda item, purpose: True)
    first.manager.working.add(first.manager.cold.items()[0])
    with repo.transaction(): repo.apply_mutation(first.freeze())
    first.close()
    second = service.load(cid, 0, 'scope', eligible=lambda item, purpose: True)
    live = second.manager.working.items()[0]
    assert second.manager.semantic.backend.get(live.id) is live
    second.close()

def test_publish_memory_receipt_is_atomic_and_lease_fenced(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService, MemoryLease
    from knowpath_backend.learning.workers.model_tasks import LeaseLost
    repo, mid, cid, _, _ = store
    lease = MemoryLease(repo, mid, 'executor')
    assert lease.acquire()
    session = MemorySessionService(repo).load(cid, 0, 'scope', eligible=lambda item, purpose: True)
    mutation = session.freeze(project_message_ids=[mid])
    with pytest.raises(RuntimeError):
        with repo.transaction():
            session.publish(mid, lease, mutation)
            raise RuntimeError('business commit failed')
    assert repo.get_commit('turn:' + mid) is None
    assert repo.items_for_turn(mid) == []
    with repo.transaction(): receipt = session.publish(mid, lease, mutation)
    assert repo.get_commit('turn:' + mid)['receipt'] == receipt
    with repo.transaction(): assert session.publish(mid, lease, mutation) == receipt
    assert repo.get_space()['revision'] == 1
    assert len(repo.items_for_turn(mid)) == 2
    session.close()

from knowpath_backend.test.test_learning_state_persistence import workspace

def test_message_entrypoint_commits_answer_memory_and_receipt(workspace):
    factory, sid, _, _ = workspace
    state = factory()
    result = state.send_message(sid, {'message': 'Explain functions'})
    business = state.message_service.repository
    message = business.records('messages', space_id=sid)[0]
    memory = business.agent_memory_factory(sid)
    assert message['status'] == 'completed'
    assert memory.get_commit('turn:' + message['id']) is not None
    assert len(memory.items_for_turn(message['id'])) == 2
    assert memory.get_checkpoint(message['conversation_id'], message['snapshot']['memory_scope']['context_epoch']) is not None
    assert len([e for e in state.events_for(result['run_id']) if e['event'] == 'message.completed']) == 1

def test_worker_waiting_for_space_preserves_attempt_budget(workspace):
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    from knowpath_backend.learning.agent_memory.service import MemoryLease
    factory, sid, _, _ = workspace
    state = factory()
    result = state.message_service.send(sid, {'message':'Explain functions'}, durable=True)
    repo = state.message_service.repository
    message = repo.records('messages', space_id=sid)[0]
    memory = repo.agent_memory_factory(sid)
    lease = MemoryLease(memory, message['id'], 'occupied')
    assert lease.acquire()
    event = next(e for e in repo.records('outbox') if e['event_type']=='message.generate')
    worker = ModelTaskWorker(messages=state.message_service)
    assert worker.claim(event['id']) is None
    waiting = repo.get_record('outbox',event['id'])
    assert waiting['attempts']==0 and waiting['status']=='pending'
    assert state.get_run(result['run_id'])['status']=='queued'
    lease.release()

def test_persisted_turn_is_eligible_with_real_live_material_resolver(workspace):
    factory, sid, _, _ = workspace
    state = factory()
    first = state.send_message(sid, {'message':'Explain functions'})
    service = state.message_service
    message = service.repository.records('messages', space_id=sid)[0]
    memory = service.repository.agent_memory_factory(sid)
    item = memory.items_for_turn(message['id'])[0]
    assert item.metadata['memory_scope'] == message['snapshot']['memory_scope']
    policy = service._build_memory_policy(memory, message['snapshot']['memory_scope'], state.material_repository)
    assert policy.eligible(item, purpose='L4')

def test_worker_claim_reserves_space_until_execution(workspace):
    factory, sid, _, _ = workspace
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    from knowpath_backend.learning.agent_memory.service import MemoryLease
    state = factory()
    state.message_service.send(sid, {'message':'Explain functions'}, durable=True)
    repo = state.message_service.repository
    event = next(e for e in repo.records('outbox') if e['event_type']=='message.generate')
    worker = ModelTaskWorker(messages=state.message_service)
    claimed = worker.claim(event['id'])
    assert claimed is not None
    memory = repo.agent_memory_factory(sid)
    competitor = MemoryLease(memory, event['aggregate_id'], 'competitor')
    assert competitor.acquire() is False
    worker.execute(claimed)
    assert repo.get_record('outbox', event['id'])['status']=='completed'

def test_entrypoint_uses_generic_write_and_tick_once_outside_transaction(workspace, monkeypatch):
    from knowpath_backend.memory.manager import MemoryManager
    factory, sid, _, _ = workspace
    state = factory()
    seen = []
    original_write, original_tick = MemoryManager.write, MemoryManager.tick
    def write(manager, *args, **kwargs):
        assert not state.material_repository._transaction_active.get() if hasattr(state.material_repository, '_transaction_active') else not state.material_repository.unit_of_work.active
        seen.append(('write', kwargs.get('role', 'user')))
        return original_write(manager, *args, **kwargs)
    def tick(manager, *args, **kwargs):
        assert not state.material_repository._transaction_active.get() if hasattr(state.material_repository, '_transaction_active') else not state.material_repository.unit_of_work.active
        seen.append(('tick', None))
        return original_tick(manager, *args, **kwargs)
    monkeypatch.setattr(MemoryManager, 'write', write)
    monkeypatch.setattr(MemoryManager, 'tick', tick)
    result = state.send_message(sid, {'message':'Explain functions'})
    message = state.message_service.repository.records('messages', space_id=sid)[0]
    assert seen == [('write','user'),('write','assistant'),('tick',None)]
    state.message_service.generate(message['id'])
    assert len(seen)==3

def test_sql_restart_l3_accumulates_and_promotes_complete_turn(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    from knowpath_backend.memory import MemoryConfig
    repo, mid, cid, _, _ = store
    with repo.transaction(): repo.project_completed(mid)
    config = MemoryConfig(tick_mode='sync', conflict_strength='off', l3_promote_min_hits=2, l3_promote_min_score=.5)
    first = MemorySessionService(repo).load(cid, 0, 'scope', eligible=lambda item, purpose: True, config=config)
    item = first.manager.cold.items()[0]
    first.manager.recall_buffer.record_hit(item.id, .8, 0)
    with repo.transaction(): repo.apply_mutation(first.freeze())
    first.close()
    restored = type(repo)(repo.materials if hasattr(repo, 'materials') else repo.unit_of_work, repo.space_id)
    second = MemorySessionService(restored).load(cid, 0, 'scope', eligible=lambda item, purpose: True, config=config)
    second.manager.recall_buffer.record_hit(item.id, .8, 0)
    second.manager.tick()
    assert {it.role for it in second.manager.working.items() if it.turn_id == mid} == {'user', 'assistant'}
    with restored.transaction(): restored.apply_mutation(second.freeze())
    second.close()
    third = MemorySessionService(restored).load(cid, 0, 'scope', eligible=lambda item, purpose: True, config=config)
    assert {it.role for it in third.manager.working.items() if it.turn_id == mid} == {'user','assistant'}
    third.close()

@pytest.mark.parametrize('option', ['whole_turns', 'cold_backend', 'context_policy'])
def test_load_rejects_constructor_bypass_before_reading_store(store, monkeypatch, option):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    repo, _, cid, _, _ = store
    monkeypatch.setattr(repo, '_locked_space', lambda: pytest.fail('invalid configuration touched storage'))
    with pytest.raises(ValueError):
        MemorySessionService(repo).load(cid, 0, 'scope', eligible=lambda item, purpose: True, **{option: None})

def test_diagnose_identity(workspace):
    from knowpath_backend.memory.dependencies import source_identity
    from knowpath_backend.memory.item import MemoryItem
    factory,sid,_,_=workspace
    state=factory(); state.send_message(sid,{'message':'Explain functions'})
    m=state.message_service.repository.records('messages',space_id=sid)[0]
    repo=state.message_service.repository.agent_memory_factory(sid)
    cp=repo.get_checkpoint(m['conversation_id'],m['snapshot']['memory_scope']['context_epoch'])
    hot=MemoryItem.model_validate(cp.state['l1'][0]); cold=repo.get_item(hot.id)
    assert source_identity(hot)==source_identity(cold), repr((source_identity(hot),source_identity(cold)))



def test_publish_failure_rolls_back_business_memory_and_events(workspace, monkeypatch):
    from knowpath_backend.learning.agent_memory.service import MemorySession
    factory,sid,_,_=workspace
    state=factory(); repo=state.message_service.repository
    result=state.send_message(sid,{'message':'Explain functions'},dispatch=lambda *args:None)
    message=repo.records('messages',space_id=sid)[0]
    original=MemorySession.publish
    def fail(session,*args,**kwargs):
        original(session,*args,**kwargs)
        raise RuntimeError('injected publication failure')
    monkeypatch.setattr(MemorySession,'publish',fail)
    with pytest.raises(RuntimeError,match='injected publication'):
        state.message_service.generate(message['id'])
    memory=repo.agent_memory_factory(sid)
    assert repo.get_record('messages',message['id'])['status']=='generating'
    assert memory.get_commit('turn:'+message['id']) is None
    assert memory.items_for_turn(message['id'])==[]
    assert memory.get_checkpoint(message['conversation_id'],message['snapshot']['memory_scope']['context_epoch']) is None
    assert not any(e['event'] in {'message.completed','run.completed'} for e in state.events_for(result['run_id']))

def test_uncertain_success_recovers_receipt_before_model_replay(workspace, monkeypatch):
    factory,sid,_,_=workspace
    state=factory(); service=state.message_service
    result=state.send_message(sid,{'message':'Explain functions'},dispatch=lambda *args:None)
    message=service.repository.records('messages',space_id=sid)[0]
    original=service.commands._execute
    def lost(op,*args,**kwargs):
        response=original(op,*args,**kwargs)
        if op=='message.publish':raise ConnectionError('commit acknowledged locally, connection lost')
        return response
    monkeypatch.setattr(service.commands,'_execute',lost)
    with pytest.raises(ConnectionError):service.generate(message['id'])
    restarted=factory(); calls=[]
    restarted.message_service.generator.generate=lambda snapshot:calls.append(snapshot)
    restarted.message_service.generate(message['id'])
    assert calls==[]
    memory=service.repository.agent_memory_factory(sid)
    assert memory.get_commit('turn:'+message['id'])
    assert len([e for e in restarted.events_for(result['run_id']) if e['event']=='message.completed'])==1

def test_load_resolves_checkpoint_references_without_full_space_scan(store, monkeypatch):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    repo,mid,cid,_,_=store
    with repo.transaction():repo.project_completed(mid)
    first=MemorySessionService(repo).load(cid,0,'scope',eligible=lambda item,purpose:True)
    item=first.manager.cold.items()[0]
    first.manager.recall_buffer.record_hit(item.id,.8,0)
    with repo.transaction():repo.apply_mutation(first.freeze())
    first.close()
    monkeypatch.setattr(repo,'list_items',lambda:pytest.fail('load scanned all historical items'))
    second=MemorySessionService(repo).load(cid,0,'scope',eligible=lambda item,purpose:True)
    assert second.manager.semantic.get(item.id).id==item.id
    assert second.freeze().checkpoint.space_id == repo.space_id
    second.close()

def test_cached_reads_cannot_publish_after_deletion_generation_changes(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService, MemoryLease
    from knowpath_backend.learning.agent_memory.repository import InvalidMutation
    repo,mid,cid,_,_=store
    with repo.transaction():repo.project_completed(mid)
    lease=MemoryLease(repo,mid,'deletion-race');assert lease.acquire()
    session=MemorySessionService(repo).load(cid,0,'scope',eligible=lambda item,purpose:True)
    matches=session.manager.semantic.search('original')
    assert matches
    cached=session.manager.cold.items()[0]
    with repo.transaction():repo.forget_item(cached.id)
    # Cached values remain local; the durable generation fence is decisive.
    assert session.manager.cold.get(cached.id) is cached
    session.manager.semantic.search('original')
    with repo.transaction(), pytest.raises(InvalidMutation):
        session.publish(mid,lease,session.freeze())
    assert repo.get_commit('turn:'+mid) is None
    assert repo.get_checkpoint(cid,0) is None
    assert repo.get_item(cached.id) is None
    session.close();lease.release()

def test_uncached_reads_reject_changed_deletion_generation(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    from knowpath_backend.learning.workers.model_tasks import LeaseLost
    repo,mid,cid,_,_=store
    with repo.transaction():repo.project_completed(mid)
    item=repo.items_for_turn(mid)[0]
    session=MemorySessionService(repo).load(cid,0,'scope',eligible=lambda item,purpose:True)
    with repo.transaction():repo.forget_item(item.id)
    with pytest.raises(LeaseLost):session.manager.semantic.search('original')
    session.close()

def test_source_eligibility_never_runs_under_memory_transaction(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    repo,mid,cid,_,_=store
    with repo.transaction():repo.project_completed(mid)
    checks=[]
    def eligible(item,purpose):
        assert not repo.active, 'source authorization inverted memory/source locks'
        checks.append(purpose)
        return True
    first=MemorySessionService(repo).load(cid,0,'scope',eligible=eligible)
    item=first.manager.cold.items()[0]
    first.manager.working.add(item)
    with repo.transaction():repo.apply_mutation(first.freeze())
    first.close()
    second=MemorySessionService(repo).load(cid,0,'scope',eligible=eligible)
    assert second.manager.working.items()
    second.manager.semantic.search('original')
    assert checks
    second.close()

def test_lazy_reads_reject_changed_revision_and_preserve_historical_search(store):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    from knowpath_backend.learning.workers.model_tasks import LeaseLost
    from knowpath_backend.memory.contracts import MemoryMutation
    repo,mid,cid,_,_=store
    with repo.transaction():repo.project_completed(mid)
    session=MemorySessionService(repo).load(cid,0,'scope',eligible=lambda item,purpose:True)
    assert session.manager.semantic.search('original')
    session.close()
    second=MemorySessionService(repo).load(cid,0,'scope',eligible=lambda item,purpose:True)
    with repo.transaction():repo.apply_mutation(MemoryMutation())
    with pytest.raises(LeaseLost):second.manager.semantic.search('original')
    second.close()

def test_real_artifact_span_dependency_validates_hash_and_coordinates(workspace):
    from knowpath_backend.learning.rag.scope import legacy_span
    from knowpath_backend.learning.agent_memory.repository import digest
    from copy import deepcopy
    factory,sid,_,_=workspace
    state=factory();state.send_message(sid,{'message':'Explain functions'})
    service=state.message_service;message=service.repository.records('messages',space_id=sid)[0]
    memory=service.repository.agent_memory_factory(sid);item=memory.items_for_turn(message['id'])[0]
    binding=state.get_space(sid)['bindings'][0]
    version=state.material_repository.get_version(binding['material_version_id'])
    span=legacy_span(version.id,version.chunks[0]).model_dump(mode='json')
    candidate=item.model_copy(deep=True);candidate.metadata.pop('source_ref',None)
    candidate.metadata['source_dependencies']=[dict(span,material_id=binding['material_id'])]
    policy=service._build_memory_policy(memory,message['snapshot']['memory_scope'],state.material_repository)
    assert policy.eligible(candidate,purpose='L4')
    for changes in ({'artifact_hash':'a'*64},{'end':len(version.chunks[0].text)+1},{'block':'missing'}):
        bad=candidate.model_copy(deep=True);bad.metadata['source_dependencies'][0].update(changes)
        assert not policy.eligible(bad,purpose='L4')

def test_derived_l0_and_l2_preserve_qualified_scope_across_restart(workspace):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    factory,sid,_,_=workspace
    state=factory();state.send_message(sid,{'message':'Explain functions'})
    service=state.message_service;message=service.repository.records('messages',space_id=sid)[0]
    scope=message['snapshot']['memory_scope'];repo=service.repository.agent_memory_factory(sid)
    policy=service._build_memory_policy(repo,scope,state.material_repository)
    eligible=lambda item,purpose:policy.eligible(item,purpose=purpose)
    session=MemorySessionService(repo).load(message['conversation_id'],scope['context_epoch'],scope['fingerprint'],eligible=eligible)
    source=session.manager.working.items()[0]
    card=session.manager.remember('Functions group behavior',metadata=source.metadata)
    assert policy.eligible(card,purpose='L0')
    session.manager.summary.add(source)
    summary=session.manager.summary.flush()
    assert policy.eligible(summary,purpose='L2')
    with repo.transaction():repo.apply_mutation(session.freeze())
    session.close()
    restarted=MemorySessionService(repo).load(message['conversation_id'],scope['context_epoch'],scope['fingerprint'],eligible=eligible)
    assert any(value.id==card.id for value in restarted.manager.playbook.all_active())
    assert restarted.manager.summary.get(summary.id).id==summary.id
    assert policy.eligible(restarted.manager.summary.get(summary.id),purpose='L2')
    restarted.close()

def test_same_epoch_multiturn_summary_remains_eligible(workspace):
    from knowpath_backend.learning.agent_memory.service import MemorySessionService
    factory,sid,_,_=workspace
    state=factory();state.send_message(sid,{'message':'Explain functions'})
    service=state.message_service
    first=service.repository.records('messages',space_id=sid)[0]
    state.send_message(sid,{'message':'Explain functions again','session_id':first['conversation_id']})
    messages=sorted(service.repository.records('messages',space_id=sid),key=lambda row:row['sequence'])
    current=messages[-1];scope=current['snapshot']['memory_scope']
    assert messages[0]['snapshot']['memory_scope']['context_epoch']==scope['context_epoch']
    assert messages[0]['snapshot']['memory_scope']['receive_sequence']!=scope['receive_sequence']
    repo=service.repository.agent_memory_factory(sid)
    policy=service._build_memory_policy(repo,scope,state.material_repository)
    eligible=lambda item,purpose:policy.eligible(item,purpose=purpose)
    session=MemorySessionService(repo).load(current['conversation_id'],scope['context_epoch'],scope['fingerprint'],eligible=eligible)
    for message in messages:session.manager.summary.add(repo.items_for_turn(message['id'])[0])
    summary=session.manager.summary.flush()
    assert policy.eligible(summary,purpose='L2')
    with repo.transaction():repo.apply_mutation(session.freeze())
    session.close()
    restarted=MemorySessionService(repo).load(current['conversation_id'],scope['context_epoch'],scope['fingerprint'],eligible=eligible)
    assert policy.eligible(restarted.manager.summary.get(summary.id),purpose='L2')
    assert set(summary.metadata['source_item_ids']) >= {repo.items_for_turn(message['id'])[0].id for message in messages}
    assert len(summary.metadata['source_receipts']) == 2
    # A rolling update retains both old contributors and the new receipt.
    restarted.manager.summary.add(repo.items_for_turn(messages[-1]['id'])[1])
    rolled=restarted.manager.summary.flush()
    assert policy.eligible(rolled,purpose='L2')
    assert len(rolled.metadata['source_receipts']) == 2
    foreign=repo.items_for_turn(messages[-1]['id'])[0].model_copy(deep=True)
    foreign.metadata['memory_scope']['context_epoch'] += 1
    restarted.manager.summary.add(foreign)
    mixed=restarted.manager.summary.flush()
    assert not policy.eligible(mixed,purpose='L2')
    # Returning to A cannot erase the earlier mixed-epoch contributor.
    restarted.manager.summary.add(repo.items_for_turn(messages[-1]['id'])[0])
    assert not policy.eligible(restarted.manager.summary.flush(),purpose='L2')
    restarted.close()

def test_transitive_dependency_requalifies_real_item_and_message_scope(workspace):
    factory,sid,_,_=workspace
    state=factory();state.send_message(sid,{'message':'Explain functions'})
    service=state.message_service;message=service.repository.records('messages',space_id=sid)[0]
    repo=service.repository.agent_memory_factory(sid);item=repo.items_for_turn(message['id'])[0]
    policy=service._build_memory_policy(repo,message['snapshot']['memory_scope'],state.material_repository)
    for dependency in ({'item_id':item.id},{'message_id':message['id']}):
        candidate=item.model_copy(deep=True);candidate.metadata.pop('source_ref',None)
        candidate.metadata['source_dependencies']=[dependency]
        assert policy.eligible(candidate,purpose='L4')
        current=dict(message['snapshot']['memory_scope']);current['context_epoch']+=1
        # Recall may cross epochs, but cannot cross a different immutable scope.
        current['identity']=dict(current['identity'],answer_mode='general')
        from knowpath_backend.learning.agent_memory.scope import scope_fingerprint
        current['fingerprint']=scope_fingerprint(current['identity'])
        candidate.metadata['memory_scope']=current
        changed=service._build_memory_policy(repo,current,state.material_repository)
        assert not changed.eligible(candidate,purpose='L4')
