import json
import pytest
from knowpath_backend.test.test_study_lessons import setup_lesson, Generator
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker

def completed_section(kind,source):
    return {'kind':kind,'markdown':'streamed '+kind,'citation_ids':[source]}

def test_sections_stream_before_model_finishes_and_each_read_audits_sources():
    state,sid,node,payload=setup_lesson()
    service=state.lesson_service
    created=service.create(sid,node,payload)
    observed=[]
    class Streaming:
        def stream(self,messages):
            source=json.loads(messages[-1]['content'])['sources'][0]['id']
            yield '{"sections":['+json.dumps(completed_section('core',source))
            observed.extend(state.events_for(created['run_id']))
            assert service.repository.get_record('study_lessons',created['lesson_id'])['status']=='generating'
            for kind in ('explanation','examples','pitfalls'):
                yield ','+json.dumps(completed_section(kind,source))
            yield ']}'
    service.generator=Streaming()
    assert ModelTaskWorker(lessons=service).run_once()
    events=[e for e in observed if e['event']=='lesson.section']
    assert events and events[-1]['data']['sections'][0]['kind']=='core'
    assert service.get(created['lesson_id'])['status']=='completed'

def test_invalid_stream_section_never_released_and_failed_drafts_are_cleared():
    state,sid,node,payload=setup_lesson()
    service=state.lesson_service;created=service.create(sid,node,payload)
    seen=[]
    class BadStream:
        def stream(self,messages):
            source=json.loads(messages[-1]['content'])['sources'][0]['id']
            yield '{"sections":['+json.dumps(completed_section('core',source))
            seen.extend(state.events_for(created['run_id']))
            yield ','+json.dumps(completed_section('explanation','invented'))+']}'
    service.generator=BadStream();ModelTaskWorker(lessons=service,max_attempts=1).run_once()
    assert any(e['event']=='lesson.section' for e in seen)
    final=state.events_for(created['run_id'])
    assert not any(e['event']=='lesson.section' and e.get('data',{}).get('sections') for e in final)
    assert any(e['event']=='lesson.reset' for e in final)
    assert service.get(created['lesson_id'])['sections']==[]

def test_lesson_stream_run_reads_mark_new_active_assessment_assisted():
    state,sid,node,payload=setup_lesson()
    service=state.lesson_service;created=service.create(sid,node,payload)
    topics=service.repository.get_record('study_lessons',created['lesson_id'])['snapshot']['topic_ids']
    class AuditedStream:
        def stream(self,messages):
            source=json.loads(messages[-1]['content'])['sources'][0]['id']
            yield '{"sections":['+json.dumps(completed_section('core',source))
            active=state.create_assessment(sid,{'kind':'practice','topic_ids':topics,'question_count':5,'difficulty_mix':{'easy':1.0,'medium':0.0,'hard':0.0}})
            assert not any(q.get('assisted') for q in service.repository.get_record('assessments',active['id'])['questions'])
            state.get_run(created['run_id'])
            assert any(q.get('assisted') for q in service.repository.get_record('assessments',active['id'])['questions'])
            for kind in ('explanation','examples','pitfalls'):yield ','+json.dumps(completed_section(kind,source))
            yield ']}'
    service.generator=AuditedStream();ModelTaskWorker(lessons=service,max_attempts=1).run_once()
    assert service.get(created['lesson_id'])['status']=='completed'

def test_message_provisional_deltas_are_audited_while_generating():
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    state,sid,node,payload=setup_lesson()
    state.message_service.retriever=KeywordRetriever()
    captured=[]
    class StreamAnswer:
        section_streaming=True
        def stream(self,snapshot,on_text=None,*,on_section):
            on_section('grounded_text','draft explanation')
            run=state.message_service.repository.records('messages',space_id=sid)[0]['run_id']
            active=state.create_assessment(sid,{'kind':'practice','topic_ids':[snapshot['sources'][0]['topic_id']],'question_count':5,'difficulty_mix':{'easy':1.0,'medium':0.0,'hard':0.0}})
            captured.extend(state.events_for(run))
            assert any(q.get('assisted') for q in state.assessment_service.repository.get_record('assessments',active['id'])['questions'])
            return {'text':'draft explanation','grounded_text':'draft explanation','citation_ids':[snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator=StreamAnswer()
    sent=state.send_message(sid,{'message':'Explain sources'})
    assert state.run_service.get(sent['run_id'])['status']=='succeeded'
    assert any(e['event']=='message.delta' for e in captured)

@pytest.mark.parametrize('storage',['memory','sql'])
def test_deleted_lesson_sources_scrub_stream_events_even_from_prior_retry_runs(storage,tmp_path):
    from sqlalchemy import create_engine
    from knowpath_backend.learning.persistence.db import init_db
    from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
    repo=None;engine=None
    if storage=='sql':
        engine=create_engine(f"sqlite+pysqlite:///{tmp_path/'stream-delete.db'}");init_db(engine)
        repo=SqlAlchemyMaterialRepository(engine)
    state,sid,node,payload=setup_lesson(repo)
    service=state.lesson_service;first=service.create(sid,node,payload)
    class Failure:
        def stream(self,messages):
            source=json.loads(messages[-1]['content'])['sources'][0]['id']
            yield '{"sections":['+json.dumps(completed_section('core',source))
            raise RuntimeError('provider unavailable')
    service.generator=Failure();ModelTaskWorker(lessons=service,max_attempts=1).run_once()
    retry=service.retry(first['lesson_id'])
    service.generator=Generator();ModelTaskWorker(lessons=service).run_once()
    source=service.repository.get_record('study_lessons',first['lesson_id'])['snapshot']['sources'][0]['material_id']
    service.delete_material(source)
    for run_id in (first['run_id'],retry['run_id']):
        assert not any(e['event']=='lesson.section' and (e.get('data') or {}).get('sections') for e in state.events_for(run_id))
    if engine:engine.dispose()

def test_cancel_during_stream_clears_prior_sections_and_prevents_late_section():
    state,sid,node,payload=setup_lesson()
    service=state.lesson_service;first=service.create(sid,node,payload)
    class Cancel:
        def stream(self,messages):
            source=json.loads(messages[-1]['content'])['sources'][0]['id']
            yield '{"sections":['+json.dumps(completed_section('core',source))
            state.cancel_run(first['run_id'])
            assert not any(e['event']=='lesson.section' and e['data'].get('sections') for e in state.events_for(first['run_id']))
            yield ','+json.dumps(completed_section('explanation',source))
            yield ']}'
    service.generator=Cancel();ModelTaskWorker(lessons=service,max_attempts=1).run_once()
    assert service.get(first['lesson_id'])['sections']==[]
    assert not any(e['event']=='lesson.section' and e['data'].get('sections') for e in state.events_for(first['run_id']))

def test_message_failed_stream_redacts_provisional_deltas_from_replay():
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    state,sid,node,payload=setup_lesson()
    state.message_service.retriever=KeywordRetriever()
    class Failure:
        section_streaming=True
        def stream(self,snapshot,on_text=None,*,on_section):
            on_section('grounded_text','should be withdrawn')
            return {'text':'invalid','citation_ids':['invented']}
    state.message_service.generator=Failure()
    sent=state.send_message(sid,{'message':'Explain sources'})
    assert state.run_service.get(sent['run_id'])['status']=='failed'
    assert not any(e['event']=='message.delta' and e['data'].get('delta') for e in state.events_for(sent['run_id']))
    assert any(e['event']=='message.reset' for e in state.events_for(sent['run_id']))

def test_lesson_http_sse_replays_validated_sections_and_audits_sources():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api.application import create_app
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    state.lesson_service.generator=Generator();ModelTaskWorker(lessons=state.lesson_service).run_once()
    topics=state.lesson_service.repository.get_record('study_lessons',first['lesson_id'])['snapshot']['topic_ids']
    active=state.create_assessment(sid,{'kind':'practice','topic_ids':topics,'question_count':5,'difficulty_mix':{'easy':1.0,'medium':0.0,'hard':0.0}})
    app=create_app(service=state.material_service);app.state.learning_state=state
    with TestClient(app) as client:response=client.get('/api/v1/runs/'+first['run_id']+'/events')
    assert response.status_code==200 and 'event: lesson.section' in response.text
    assert 'Grounded explanation' in response.text
    assert any(q.get('assisted') for q in state.assessment_service.repository.get_record('assessments',active['id'])['questions'])

def test_expired_lease_cannot_replay_lesson_drafts():
    from datetime import datetime,timedelta,timezone
    state,sid,node,payload=setup_lesson()
    service=state.lesson_service;first=service.create(sid,node,payload)
    class Expire:
        def stream(self,messages):
            source=json.loads(messages[-1]['content'])['sources'][0]['id']
            yield '{"sections":['+json.dumps(completed_section('core',source))
            row=service.repository.get_record('study_lessons',first['lesson_id'])
            event=service.repository.get_record('outbox',row['event_id'])
            event['lease_until']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
            service.repository.put_record('outbox',event)
            assert not any(e['event']=='lesson.section' and e['data'].get('sections') for e in state.events_for(first['run_id']))
            yield ']}'
    service.generator=Expire();ModelTaskWorker(lessons=service,max_attempts=1).run_once()
    assert service.get(first['lesson_id'])['sections']==[]

def test_tiny_answer_tokens_are_coalesced_without_losing_first_text_or_tail():
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    state,sid,node,payload=setup_lesson();state.message_service.retriever=KeywordRetriever()
    class TinyTokens:
        def stream(self,snapshot,on_text):
            for _ in range(1000):on_text('x')
            return {'text':'x'*1000,'citation_ids':[snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator=TinyTokens()
    sent=state.send_message(sid,{'message':'Explain sources'})
    deltas=[e for e in state.events_for(sent['run_id']) if e['event']=='message.delta']
    assert ''.join(e['data']['delta'] for e in deltas)=='x'*1000
    assert len(deltas)<=20

def test_stream_replay_audits_assessments_in_another_space_using_same_sources():
    state,sid,node,payload=setup_lesson()
    service=state.lesson_service;first=service.create(sid,node,payload)
    frozen=service.repository.get_record('study_lessons',first['lesson_id'])['snapshot']
    other=state.create_space({'name':'Shared sources','material_ids':list({s['material_id'] for s in frozen['sources']})})
    class SharedStream:
        def stream(self,messages):
            source=json.loads(messages[-1]['content'])['sources'][0]['id']
            yield '{"sections":['+json.dumps(completed_section('core',source))
            active=state.create_assessment(other['id'],{'kind':'practice','topic_ids':frozen['topic_ids'],'question_count':5,'difficulty_mix':{'easy':1.0,'medium':0.0,'hard':0.0}})
            state.events_for(first['run_id'])
            assert any(q.get('assisted') for q in service.repository.get_record('assessments',active['id'])['questions'])
            for kind in ('explanation','examples','pitfalls'):yield ','+json.dumps(completed_section(kind,source))
            yield ']}'
    service.generator=SharedStream();ModelTaskWorker(lessons=service,max_attempts=1).run_once()
    assert service.get(first['lesson_id'])['status']=='completed'

def test_automatic_message_retry_emits_reset_and_clears_first_attempt_text():
    from datetime import datetime,timedelta,timezone
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    from knowpath_backend.learning.conversations.generation import MessageGenerationError
    state,sid,node,payload=setup_lesson();state.message_service.retriever=KeywordRetriever()
    class Retry:
        calls=0
        section_streaming=True
        def stream(self,snapshot,on_text=None,*,on_section):
            self.calls+=1
            on_section('grounded_text','failed draft' if self.calls==1 else 'second attempt')
            if self.calls==1:raise MessageGenerationError('MODEL_UNAVAILABLE')
            return {'text':'second attempt','grounded_text':'second attempt','citation_ids':[snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator=Retry()
    sent=state.send_message(sid,{'message':'Explain sources'},durable=True)
    worker=ModelTaskWorker(messages=state.message_service)
    assert worker.run_once()
    first_events=state.events_for(sent['run_id'])
    assert any(e['event']=='message.reset' for e in first_events)
    assert not any(e['event']=='message.delta' and e['data'].get('delta') for e in first_events)
    worker.clock=lambda:datetime.now(timezone.utc)+timedelta(seconds=10)
    assert worker.run_once()
    assert state.run_service.get(sent['run_id'])['status']=='succeeded'
    assert ''.join(e['data']['delta'] for e in state.events_for(sent['run_id']) if e['event']=='message.delta')=='### 资料依据\n\nsecond attempt'

def test_expired_message_lease_cannot_replay_provisional_deltas():
    from datetime import datetime,timedelta,timezone
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    state,sid,node,payload=setup_lesson();state.message_service.retriever=KeywordRetriever()
    seen=[]
    class ExpireAnswer:
        def stream(self,snapshot,on_text):
            on_text('expired draft')
            message=state.message_service.repository.records('messages',space_id=sid)[0]
            event=state.message_service.repository.records('outbox',aggregate_id=message['id'])[0]
            event['lease_until']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
            state.message_service.repository.put_record('outbox',event)
            seen.extend(state.events_for(message['run_id']))
            return {'text':'expired draft','citation_ids':[snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator=ExpireAnswer()
    state.send_message(sid,{'message':'Explain sources'},durable=True)
    ModelTaskWorker(messages=state.message_service,max_attempts=1).run_once()
    assert not any(e['event']=='message.delta' and e['data'].get('delta') for e in seen)
