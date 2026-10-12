import pytest
from sqlalchemy import create_engine
from knowpath_backend.test.test_learning_plans_sessions import seed
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker

def setup_lesson(repo=None):
    state, sid, _ = seed(repo or InMemoryMaterialRepository())
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
    state.assessment_service.generator=FixedQuestions()
    plan = state.create_plan(sid, {})
    task = state.plan_sessions._space_plans(sid)[0]['tasks'][0]
    payload = {'plan_id': plan['plan_id'], 'task_id': task['id']}
    return state, sid, task['node_id'], payload

class Generator:
    def generate_json(self, messages):
        import json
        snapshot = json.loads(messages[-1]['content'])
        return {'sections': [{'kind': k, 'markdown': 'Grounded explanation', 'citation_ids': [snapshot['sources'][0]['id']]} for k in ('core','explanation','examples','pitfalls')]}

def test_lesson_reuses_and_versions_success_without_finishing_learning():
    state, sid, node, payload = setup_lesson()
    lessons = state.lesson_service
    first = lessons.create(sid, node, payload, 'lesson-one')
    assert lessons.create(sid, node, payload) == first
    lessons.generator = Generator()
    assert ModelTaskWorker(lessons=lessons).run_once()
    body = lessons.get(first['lesson_id'])
    assert body['status'] == 'completed' and len(body['sections']) == 4
    assert lessons.create(sid,node,payload)['lesson_id'] == first['lesson_id']
    second = lessons.create(sid,node,{**payload,'regenerate':True})
    assert second['lesson_id'] != first['lesson_id']
    assert lessons.get(first['lesson_id'])['sections'] == body['sections']
    assert not state.plan_sessions._space_sessions(sid)

def test_lesson_sql_survives_restart(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path/'lessons.db'}")
    init_db(engine)
    state,sid,node,payload = setup_lesson(SqlAlchemyMaterialRepository(engine))
    first = state.lesson_service.create(sid,node,payload)
    state.lesson_service.generator = Generator()
    ModelTaskWorker(lessons=state.lesson_service).run_once()
    restored = LearningState(SqlAlchemyMaterialRepository(engine))
    assert restored.lesson_service.get(first['lesson_id'])['status'] == 'completed'

@pytest.mark.parametrize('sql', [False, True])
def test_lesson_generation_uses_frozen_active_session_on_both_repositories(tmp_path, sql):
    if sql:
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path/'session-lesson.db'}")
        init_db(engine)
        repo = SqlAlchemyMaterialRepository(engine)
    else:
        repo = InMemoryMaterialRepository()
    state, sid, node, payload = setup_lesson(repo)
    session = state.plan_sessions.start_session(payload['plan_id'], payload['task_id'])
    created = state.lesson_service.create(sid, node, {**payload, 'learning_session_id': session['session_id']})
    assert created['lesson_id']

def test_lesson_invalid_citations_fail_and_retry_new_run():
    state,sid,node,payload = setup_lesson()
    first = state.lesson_service.create(sid,node,payload)
    class Bad:
        def generate_json(self,messages): return {'sections':[{'kind':k,'markdown':'invented','citation_ids':['fake']} for k in ('core','explanation','examples','pitfalls')]}
    state.lesson_service.generator=Bad()
    ModelTaskWorker(lessons=state.lesson_service,max_attempts=1).run_once()
    assert state.lesson_service.get(first['lesson_id'])['error']['code']=='MODEL_INVALID_RESPONSE'
    retry=state.lesson_service.retry(first['lesson_id'],'retry-one')
    assert retry['run_id']!=first['run_id']
    assert state.lesson_service.retry(first['lesson_id'],'retry-one')==retry

def test_lesson_deletion_during_generation_prevents_late_publish():
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    service=state.lesson_service
    material=service.repository.get_record('study_lessons',first['lesson_id'])['snapshot']['sources'][0]['material_id']
    class DeleteDuring(Generator):
        def generate_json(self,messages):
            output=super().generate_json(messages)
            service.delete_material(material)
            return output
    service.generator=DeleteDuring()
    ModelTaskWorker(lessons=service).run_once()
    row=service.repository.get_record('study_lessons',first['lesson_id'])
    assert row['snapshot']=={} and row['sections']==[] and row['status']=='cancelled'

def test_lesson_read_audits_active_assessment_again_and_metadata_does_not():
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    state.lesson_service.generator=Generator()
    ModelTaskWorker(lessons=state.lesson_service).run_once()
    state.lesson_service.get(first['lesson_id'])
    lesson_topics=state.lesson_service.repository.get_record('study_lessons',first['lesson_id'])['snapshot']['topic_ids']
    active=state.create_assessment(sid,{'kind':'practice','topic_ids':lesson_topics,'question_count':5,'difficulty_mix':{'easy':1.0,'medium':0.0,'hard':0.0}})
    state.lesson_service.list(sid,node)
    assert not any(q.get('assisted') for q in state.assessment_service.repository.get_record('assessments',active['id'])['questions'])
    state.lesson_service.get(first['lesson_id'])
    assert any(q.get('assisted') for q in state.assessment_service.repository.get_record('assessments',active['id'])['questions'])

def test_lesson_followup_without_session_filters_sources_and_audits_history():
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    state.lesson_service.generator=Generator()
    ModelTaskWorker(lessons=state.lesson_service).run_once()
    frozen=state.lesson_service.repository.get_record('study_lessons',first['lesson_id'])['snapshot']['sources']
    class Answer:
        def generate(self,snapshot):
            assert {s['chunk_id'] for s in snapshot['sources']}.issubset({s['chunk_id'] for s in frozen})
            return {'text':'Grounded followup','citation_ids':[snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator=Answer()
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    state.message_service.retriever=KeywordRetriever()
    class WideRag:
        def answer(self,*args,**kwargs):raise AssertionError('must not widen lesson chunks')
    state.message_service.rag_pipeline=WideRag()
    sent=state.send_message(sid,{'message':'Explain the concept','lesson_id':first['lesson_id']})
    messages=state.message_service.history(sid,sent['session_id'])
    assert messages['items'][0]['response']['text']=='Grounded followup'
    assert 'snapshot' not in messages['items'][0]
    followup=state.send_message(sid,{'message':'Another explanation','session_id':sent['session_id']})
    assert followup['session_id']==sent['session_id']
    row=state.message_service.repository.records('messages',run_id=followup['run_id'])[0]
    assert row['snapshot']['lesson_id']==first['lesson_id']

def test_lesson_api_contract_and_invalid_style():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api.application import create_app
    state,sid,node,payload=setup_lesson()
    app=create_app(service=state.material_service)
    app.state.learning_state=state
    state.lesson_service.generator=Generator()
    client=TestClient(app,headers={'Idempotency-Key':'lesson-api-default'})
    state.update_profile(sid,{'preferences':{'lesson_style':'deep','lesson_instructions':'more examples'},'expected_version':state.get_space(sid)['profile_version']})
    path=f'/api/v1/learning-spaces/{sid}/nodes/{node}/lessons'
    assert client.post(path,json={**payload,'style':'invalid'}).status_code==422
    assert client.post(path,json={**payload,'instructions':'x'*1001}).status_code==422
    created=client.post(path,json={**payload,'style':'deep','instructions':'more examples'},headers={'Idempotency-Key':'api-lesson'})
    assert created.status_code==202
    data=created.json()
    assert set(data)=={'lesson_id','run_id','status'}
    body=client.get('/api/v1/study-lessons/'+data['lesson_id']).json()
    assert body['style']=='deep' and body['status']=='completed'
    listed=client.get(path).json()
    assert listed['preferences']['lesson_style']=='deep'
    assert 'sections' not in listed['items'][0] and 'snapshot' not in listed['items'][0]

def test_lesson_scope_change_marks_history_stale_and_forbids_retry():
    from knowpath_backend.learning.errors import DomainConflict
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    state.lesson_service.generator=Generator()
    ModelTaskWorker(lessons=state.lesson_service).run_once()
    space=state.space_service.repository.get(sid)
    state.set_scope(sid,{'topic_ids':space['topic_ids'],'expected_version':space['space_version']})
    body=state.lesson_service.get(first['lesson_id'])
    assert body['stale'] and body['sections']
    with pytest.raises(DomainConflict):state.lesson_service.create(sid,node,payload)

def test_study_lesson_migration_has_single_table_and_correct_parent():
    from importlib import import_module
    migration=import_module('migrations.versions.0017_study_lessons')
    assert migration.down_revision=='0016_learning_notes'
    from knowpath_backend.learning.persistence.db import StudyLessonRow
    assert StudyLessonRow.__tablename__=='study_lessons'
    assert any(set(c.columns.keys())=={'space_id','node_id','version'} for c in StudyLessonRow.__table__.constraints)

def test_preferences_export_retains_legacy_bools_and_lesson_strings():
    state,sid,node,payload=setup_lesson()
    prefs={'example_first':True,'concise_explanations':False,'lesson_style':'examples_first','lesson_instructions':'use plain words'}
    state.update_profile(sid,{'preferences':prefs,'expected_version':state.get_space(sid)['profile_version']})
    replanned=state.create_plan(sid,{})
    task=next(t for p in state.plan_sessions._space_plans(sid) if p['id']==replanned['plan_id'] for t in p['tasks'] if not t.get('context',{}).get('historical'))
    node=task['node_id'];payload={'plan_id':replanned['plan_id'],'task_id':task['id']}
    exported=state.create_export(sid)
    result=state.export_payload(exported['export_id'])
    assert result['space']['profile']['preferences']['value']==prefs
    lesson=state.lesson_service.create(sid,node,payload)
    body=state.lesson_service.get(lesson['lesson_id'])
    assert body['style']=='examples_first' and body['instructions']=='use plain words'

def test_source_budget_reports_uncovered_topics_and_preserves_original_chunks():
    state,sid,node,payload=setup_lesson()
    service=state.lesson_service
    space,frozen=service._context(sid,node,payload)
    source=frozen['sources'][0]
    extra={**source,'id':'extra','chunk_id':'other','text':'x'*1000}
    frozen['sources'].append(extra)
    frozen['topic_ids'].append('uncovered')
    frozen['topics'].append({'id':'uncovered','name':'Second','source_refs':[extra]})
    service.max_source_chars=len(source['text'])
    service.max_source_batches=1
    bounded=service._bounded(frozen)
    assert bounded['sources']==[source]
    assert 'uncovered' in bounded['uncovered_topic_ids']

def test_archived_lesson_body_readable_generation_blocked():
    from knowpath_backend.learning.errors import DomainConflict
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    state.lesson_service.generator=Generator()
    ModelTaskWorker(lessons=state.lesson_service).run_once()
    space=state.get_space(sid)
    state.update_space(sid,{'status':'archived','expected_version':space['space_version']})
    assert state.lesson_service.get(first['lesson_id'])['sections']
    with pytest.raises(DomainConflict) as exc:state.lesson_service.create(sid,node,payload)
    assert exc.value.code=='SPACE_ARCHIVED'

def test_space_delete_removes_lessons_runs_and_prevents_idempotent_replay():
    from knowpath_backend.learning.errors import DomainNotFound
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload,'space-delete-lesson')
    state.delete_space(sid,{'confirm':True,'expected_version':state.get_space(sid)['space_version']})
    with pytest.raises(DomainNotFound):state.lesson_service.get(first['lesson_id'])
    with pytest.raises(DomainNotFound):state.run_service.get(first['run_id'])

def test_durable_lesson_runs_survive_startup_and_old_event_cannot_cancel_retry():
    from knowpath_backend.learning.startup import recover_legacy_runs
    state,sid,node,payload=setup_lesson()
    service=state.lesson_service
    first=service.create(sid,node,payload)
    old=service.repository.get_record('study_lessons',first['lesson_id'])['event_id']
    recover_legacy_runs(state.run_service,service.repository)
    assert state.run_service.get(first['run_id'])['status']=='queued'
    state.run_service.request_cancel(first['run_id']);state.run_service.acknowledge_cancel(first['run_id'])
    retry=service.retry(first['lesson_id'],'retry-after-cancel')
    worker=ModelTaskWorker(lessons=service)
    assert not worker.run_once(old)
    assert service.repository.get_record('study_lessons',first['lesson_id'])['status']=='generating'
    service.generator=Generator()
    assert worker.run_once()
    assert service.get(first['lesson_id'])['status']=='completed'

def test_retry_active_replay_remains_original_even_after_failure():
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    replay=state.lesson_service.retry(first['lesson_id'],'retry-active')
    state.run_service.request_cancel(first['run_id']);state.run_service.acknowledge_cancel(first['run_id'])
    newer=state.lesson_service.retry(first['lesson_id'])
    assert newer['run_id']!=first['run_id']
    assert state.lesson_service.retry(first['lesson_id'],'retry-active')==replay

def test_archive_during_provider_call_rejects_publication_and_lesson_writes():
    from knowpath_backend.learning.errors import DomainConflict
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    class Archive(Generator):
        def generate_json(self,messages):
            raw=super().generate_json(messages)
            state.update_space(sid,{'status':'archived','expected_version':state.get_space(sid)['space_version']})
            return raw
    state.lesson_service.generator=Archive()
    ModelTaskWorker(lessons=state.lesson_service).run_once()
    assert state.lesson_service.get(first['lesson_id'])['status']=='failed'
    with pytest.raises(DomainConflict):state.update_profile(sid,{'preferences':{'lesson_style':'plain'},'expected_version':state.get_space(sid)['profile_version']})

def test_lesson_only_preferences_keep_plan_ready_legacy_preferences_require_replan():
    state,sid,node,payload=setup_lesson()
    state.update_profile(sid,{'preferences':{'lesson_style':'deep'},'expected_version':state.get_space(sid)['profile_version']})
    assert state.get_plan(payload['plan_id'])['status']=='ready'
    state.update_profile(sid,{'preferences':{'example_first':True},'expected_version':state.get_space(sid)['profile_version']})
    assert state.get_plan(payload['plan_id'])['status']=='ready'
    assert state.get_plan(payload['plan_id'])['adjustment_pending']

def test_history_starts_with_latest_page_and_cursor_pages_older_turns():
    from knowpath_backend.learning.notes.service import now
    state,sid,node,payload=setup_lesson()
    repo=state.message_service.repository
    repo.put_record('conversations',{'id':'history-test','space_id':sid,'learning_session_id':None,'created_at':now()})
    for seq in range(1,26):
        repo.put_record('messages',{'id':f'm{seq:02}','space_id':sid,'conversation_id':'history-test','sequence':seq,'message':str(seq),'response':None,'status':'failed','run_id':'none','snapshot':{},'created_at':now()})
    latest=state.message_service.history(sid,'history-test')
    assert [m['sequence'] for m in latest['items']]==list(range(6,26))
    older=state.message_service.history(sid,'history-test',cursor=latest['next_cursor'])
    assert [m['sequence'] for m in older['items']]==list(range(1,6))
    assert older['next_cursor'] is None

def test_followup_prompt_preserves_same_lesson_history_and_discards_cross_lesson():
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    state.lesson_service.generator=Generator()
    ModelTaskWorker(lessons=state.lesson_service).run_once()
    captures=[]
    class Answer:
        def generate(self,snapshot):
            captures.append(snapshot)
            return {'text':'First explanation','citation_ids':[snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator=Answer();state.message_service.retriever=KeywordRetriever()
    sent=state.send_message(sid,{'message':'Explain this lesson','lesson_id':first['lesson_id']})
    state.send_message(sid,{'message':'Explain that example again','session_id':sent['session_id']})
    assert captures[-1]['history'][1]['content']=='First explanation'
    assert 'Grounded explanation' in captures[-1]['memory']['summary']['text']
    second=state.lesson_service.create(sid,node,{**payload,'regenerate':True})
    ModelTaskWorker(lessons=state.lesson_service).run_once()
    state.send_message(sid,{'message':'Explain new lesson','lesson_id':second['lesson_id'],'session_id':sent['session_id']})
    assert captures[-1]['history']==[]

def test_archive_during_lesson_followup_prevents_answer_publication():
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    state.lesson_service.generator=Generator();ModelTaskWorker(lessons=state.lesson_service).run_once()
    class ArchiveAnswer:
        def generate(self,snapshot):
            state.update_space(sid,{'status':'archived','expected_version':state.get_space(sid)['space_version']})
            return {'text':'Late explanation','citation_ids':[snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator=ArchiveAnswer();state.message_service.retriever=KeywordRetriever()
    sent=state.send_message(sid,{'message':'Explain lesson','lesson_id':first['lesson_id']})
    assert state.run_service.get(sent['run_id'])['status']=='failed'
    assert state.message_service.repository.records('messages',run_id=sent['run_id'])[0]['response'] is None

def test_inherited_node_keeps_completed_lesson_and_reuses_matching_context():
    state,sid,node,payload=setup_lesson()
    first=state.lesson_service.create(sid,node,payload)
    state.lesson_service.generator=Generator();ModelTaskWorker(lessons=state.lesson_service).run_once()
    replanned=state.create_plan(sid,{'rebuild_mode':'local_replan','base_plan_id':payload['plan_id'],'expected_plan_version':state.get_plan(payload['plan_id'])['version']})
    task=next(t for p in state.plan_sessions._space_plans(sid) if p['id']==replanned['id'] for t in p['tasks'] if t['node_id']==node)
    assert not state.lesson_service.get(first['lesson_id'])['stale']
    assert state.lesson_service.create(sid,node,{'plan_id':replanned['id'],'task_id':task['id']})['lesson_id']==first['lesson_id']

def test_lesson_document_selection_cannot_leak_sections_or_previous_scope():
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    state=LearningState(InMemoryMaterialRepository())
    a=state.material_service.create(filename='a.md',content=b'# Apples\n\nApples are fruits.',idempotency_key='scope-a')
    b=state.material_service.create(filename='b.md',content=b'# Bikes\n\nBikes have wheels.',idempotency_key='scope-b')
    space=state.create_space({'material_ids':[a.material.id,b.material.id]})
    sid=space['id'];topics=state.topics_for_space(sid)
    plan=state.create_plan(sid,{})
    raw=state.plan_sessions.repository.get_record('plans',plan['id'])
    task=raw['tasks'][0]
    task['topic_ids']=[t['id'] for t in topics]
    task['context']['topics']=[{'id':t['id'],'name':t['name'],'source_refs':t['source_refs']} for t in topics]
    task['context']['source_refs']=[r for t in topics for r in t['source_refs']]
    state.plan_sessions.repository.put_record('plans',raw)
    first=state.lesson_service.create(sid,task['context']['node_id'],{'plan_id':plan['id'],'task_id':task['id']})
    class MixedLesson:
        def generate_json(self,messages):
            import json
            refs=json.loads(messages[-1]['content'])['sources']
            only_a=next(s['id'] for s in refs if s['material_id']==a.material.id)
            return {'sections':[{'kind':k,'markdown':'B facts must never leak to A','citation_ids':[only_a]} for k in ('core','explanation','examples','pitfalls')]}
    state.lesson_service.generator=MixedLesson();ModelTaskWorker(lessons=state.lesson_service).run_once()
    captures=[]
    class Answer:
        def generate(self,snapshot):
            captures.append(snapshot)
            return {'text':'Selected answer','citation_ids':[snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator=Answer();state.message_service.retriever=KeywordRetriever()
    sent=state.send_message(sid,{'message':'Explain apples','lesson_id':first['lesson_id'],'material_ids':[a.material.id]})
    assert not captures[-1]['memory']['summary']
    assert {s['material_id'] for s in captures[-1]['sources']}=={a.material.id}
    state.send_message(sid,{'message':'Explain bikes','session_id':sent['session_id'],'material_ids':[b.material.id]})
    assert captures[-1]['history']==[]
    state.send_message(sid,{'message':'Explain apples again','session_id':sent['session_id'],'material_ids':[a.material.id]})
    assert captures[-1]['history']==[]

def test_archived_null_preferences_cannot_clear_saved_lesson_style():
    from knowpath_backend.learning.errors import DomainConflict
    state,sid,node,payload=setup_lesson()
    state.update_profile(sid,{'preferences':{'lesson_style':'deep'},'expected_version':state.get_space(sid)['profile_version']})
    state.update_space(sid,{'status':'archived','expected_version':state.get_space(sid)['space_version']})
    with pytest.raises(DomainConflict):state.update_profile(sid,{'preferences':None,'expected_version':state.get_space(sid)['profile_version']})
