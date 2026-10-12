import copy
import pytest
from knowpath_backend.test.test_learning_state_persistence import workspace, create
from knowpath_backend.learning.conversations.schemas import SendMessage

class SemanticAnswer:
    def __init__(self, document=False, educational=False, uncertain=False):
        self.route_calls = []; self.calls = []
        self.route = dict(document_related=document, educational=educational, topic_ids=[], scope_uncertain=uncertain, reason='semantic fixture')
    def classify(self, snapshot):
        self.route_calls.append(copy.deepcopy(snapshot)); return self.route
    def generate(self, snapshot):
        self.calls.append(copy.deepcopy(snapshot))
        return {'text':'A normal conversational answer.', 'citation_ids':[]}

def setup(workspace, **options):
    factory, sid, topic, _ = workspace
    state = factory(); model = SemanticAnswer(**options); state.message_service.generator = model
    return state, model, sid, topic

def test_contract_mode_and_null_default():
    assert SendMessage(message='Hello').answer_mode == 'auto'
    assert SendMessage(message='Hello', answer_mode=None).answer_mode == 'auto'
    with pytest.raises(ValueError): SendMessage(message='Hello', answer_mode='unknown')

def test_general_semantic_route_skips_retrieval_and_has_metadata(workspace):
    state, model, sid, _ = setup(workspace)
    state.message_service.retriever.select = lambda *a, **kw: pytest.fail('general mode retrieved')
    response = state.send_message(sid, {'message':'Hello'})
    run = state.get_run(response['run_id'])
    assert run['status'] == 'succeeded'
    done = next(e['data'] for e in run['events'] if e['event']=='message.completed')
    assert done['answer_mode']=='general' and done['citations']==[]
    assert model.calls[0]['sources']==[] and len(model.route_calls)==1
    names = [e['event'] for e in run['events']]
    assert names.index('message.route') < names.index('message.delta')
    assert state.message_service.history(sid,response['session_id'])['items'][0]['response']=={k:v for k,v in done.items() if k not in {'run_id','timestamp'}}

def test_educational_general_help_audited_on_delivery_and_replay(workspace):
    state, model, sid, _ = setup(workspace, educational=True, uncertain=True)
    assessment = create(state,sid)
    response = state.send_message(sid, {'message':'Explain this exercise in general'})
    stored = state.repository.get_record('assessments',assessment['id']) if hasattr(state,'repository') else state.assessment_service.repository.get_record('assessments',assessment['id'])
    assert all(q.get('assisted',False) for q in stored['questions'])
    assert stored['snapshot']['all_topics_assisted'] is True
    later = create(state,sid)
    state.message_service.history(sid,response['session_id'])
    assert all(q.get('assisted',False) for q in state.assessment_service.repository.get_record('assessments',later['id'])['questions'])

def test_smalltalk_does_not_assist(workspace):
    state, model, sid, _ = setup(workspace)
    assessment = create(state,sid)
    response = state.send_message(sid, {'message':'Hello'})
    assert state.get_run(response['run_id'])['status']=='succeeded'
    assert not any(q.get('assisted',False) for q in state.assessment_service.repository.get_record('assessments',assessment['id'])['questions'])

def test_classifier_failure_is_explicit(workspace):
    from knowpath_backend.learning.conversations.generation import MessageGenerationError
    state, model, sid, _ = setup(workspace)
    def failed(snapshot): raise MessageGenerationError('MESSAGE_ROUTING_FAILED')
    model.classify=failed
    response=state.send_message(sid,{'message':'Hello'})
    run=state.get_run(response['run_id'])
    assert run['error']['code']=='MESSAGE_ROUTING_FAILED' and model.calls==[]

def test_mixed_sections_require_explicit_labels_and_valid_grounding():
    from knowpath_backend.learning.conversations.adaptive import validate_adaptive_answer
    from knowpath_backend.learning.conversations.generation import MessageGenerationError
    source={'chunk_id':'c','material_id':'m','material_version_id':'v','text':'fact'}
    snapshot={'sources':[source],'answer_mode':'material','requested_answer_mode':'auto'}
    text='### Based on your material\n\nGrounded fact.\n\n### Model knowledge supplement\n\nAdditional knowledge.'
    raw={'text':text,'grounded_text':'Grounded fact.','supplement_text':'Additional knowledge.','citation_ids':['c']}
    assert validate_adaptive_answer(raw,snapshot)[2]=='mixed'
    assert validate_adaptive_answer({**raw,'text':'Different headings and punctuation'},snapshot)[2]=='mixed'
    for change in ({'citation_ids':['forged']},{'grounded_text':''}):
        with pytest.raises(MessageGenerationError):validate_adaptive_answer({**raw,**change},snapshot)


def test_route_retry_restart_reuses_persisted_semantics(workspace):
    state, model, sid, _=setup(workspace)
    response=state.send_message(sid,{'message':'Hello'},dispatch=lambda *a:None)
    row=state.message_service.repository.records('messages',space_id=sid)[0]
    original=model.generate
    def stopped(snapshot):raise RuntimeError('worker interrupted after route commit')
    model.generate=stopped
    state.message_service.generate(row['id'])
    stored=state.message_service.repository.get_record('messages',row['id'])
    assert stored['snapshot']['semantic_route']==model.route and len(model.route_calls)==1
    # Exercise persisted recovery through the same routing entrypoint without another model call.
    model.generate=original
    def no_classifier(snapshot):pytest.fail('persisted route must be reused')
    model.classify=no_classifier
    stored['status']='generating'
    state.message_service.repository.put_record('messages',stored)
    state.message_service._record_sources=lambda *a,**kw:True
    result=state.message_service._adaptive_answer(stored['id'],stored['snapshot'],stored)
    assert result[0]=='A normal conversational answer.'


def test_general_unknown_citation_is_never_published(workspace):
    state,model,sid,_=setup(workspace)
    model.generate=lambda snap:{'text':'Hello','citation_ids':['invented']}
    response=state.send_message(sid,{'message':'Hello'})
    run=state.get_run(response['run_id'])
    assert run['error']['code']=='MESSAGE_VALIDATION_FAILED'
    assert not any(e['event'] in {'message.delta','message.completed'} for e in run['events'])


def test_stream_general_assistance_is_committed_before_first_delta(workspace):
    state,model,sid,_=setup(workspace,educational=True,uncertain=True)
    assessment=create(state,sid)
    def stream(snapshot,on_text):
        on_text('A normal ')
        stored=state.assessment_service.repository.get_record('assessments',assessment['id'])
        assert all(q.get('assisted') for q in stored['questions'])
        on_text('conversational answer.')
        return {'text':'A normal conversational answer.','citation_ids':[]}
    model.stream=stream
    response=state.send_message(sid,{'message':'Explain generally'})
    run=state.get_run(response['run_id'])
    assert run['status']=='succeeded'
    assert ''.join(e['data']['delta'] for e in run['events'] if e['event']=='message.delta')=='A normal conversational answer.'
    later=create(state,sid)
    state.message_service.read_run(response['run_id'])
    assert all(q.get('assisted') for q in state.assessment_service.repository.get_record('assessments',later['id'])['questions'])


def test_omitted_null_auto_idempotency_equivalent(workspace):
    state,model,sid,_=setup(workspace)
    first=state.send_message(sid,{'message':'Hello'},idempotency_key='adaptive-key')
    assert state.send_message(sid,{'message':'Hello','answer_mode':None},idempotency_key='adaptive-key')==first
    assert state.send_message(sid,{'message':'Hello','answer_mode':'auto'},idempotency_key='adaptive-key')==first
    assert len(model.route_calls)==1


def test_cancel_during_semantic_classification_discards_result(workspace):
    state,model,sid,_=setup(workspace)
    response=state.send_message(sid,{'message':'Hello'},dispatch=lambda *a:None)
    row=state.message_service.repository.records('messages',space_id=sid)[0]
    def classify(snapshot):
        state.cancel_run(response['run_id']);return model.route
    model.classify=classify
    state.message_service.generate(row['id'])
    assert state.get_run(response['run_id'])['status']=='cancelled' and model.calls==[]

def test_configured_semantic_provider_uses_json_and_untrusted_scope(monkeypatch):
    monkeypatch.undo()
    import knowpath_backend.learning.providers.models as providers
    from knowpath_backend.learning.conversations.adaptive import classify_with_provider, validate_route
    calls=[]
    route={'document_related':False,'educational':False,'topic_ids':[],'scope_uncertain':False,'reason':'greeting'}
    class Model:
        def generate_json(self,messages):calls.append(messages);return route
    monkeypatch.setattr(providers,'chat_model',lambda settings,client=None:Model())
    snap={'message':'Hello','sources':[],'history':[{'role':'user','content':'prior'}],'route_topics':[]}
    assert validate_route(classify_with_provider(object(),None,snap),snap)==route
    assert calls[0][0]['role']=='system' and 'untrusted' in calls[0][0]['content']
    assert 'prior' in calls[0][1]['content']


def test_generating_assessment_general_assistance_survives_question_generation(workspace):
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    state,model,sid,_=setup(workspace,educational=True,uncertain=True)
    response=state.assessment_service.create(sid,{'question_count':5,'question_types':['single_choice']},dispatch=lambda *a:None,durable=True)
    pending=state.assessment_service.repository.records('assessments',space_id=sid)[0]
    state.send_message(sid,{'message':'Help me understand this exercise'})
    audited=state.assessment_service.repository.get_record('assessments',pending['id'])
    assert audited['snapshot']['all_topics_assisted'] is True
    job=state.assessment_service.repository.records('outbox',aggregate_id=pending['id'])[0]
    ModelTaskWorker(assessments=state.assessment_service).run_once(job['id'])
    done=state.assessment_service.repository.get_record('assessments',pending['id'])
    assert done['status']=='ready' and all(q.get('assisted') for q in done['questions'])

def test_confidently_unrelated_educational_help_does_not_assist(workspace):
    state,model,sid,_=setup(workspace,educational=True,uncertain=False)
    assessment=create(state,sid)
    response=state.send_message(sid,{'message':'Explain planting roses, unrelated to Python'})
    assert state.get_run(response['run_id'])['status']=='succeeded'
    assert not any(q.get('assisted') for q in state.assessment_service.repository.get_record('assessments',assessment['id'])['questions'])

def test_source_free_own_history_reaches_semantic_followup(workspace):
    state,model,sid,_=setup(workspace)
    first=state.send_message(sid,{'message':'Tell me about gardening'})
    state.send_message(sid,{'message':'How can I start it?', 'session_id':first['session_id']})
    assert model.route_calls[1]['history']==[
        {'role':'user','content':'Tell me about gardening'},
        {'role':'assistant','content':'A normal conversational answer.'}]


def test_semantic_router_never_receives_hidden_adaptive_questions(workspace):
    state,model,sid,_=setup(workspace)
    assessment=create(state,sid)
    repo=state.assessment_service.repository
    record=repo.get_record('assessments',assessment['id'])
    shown=record['questions'][0]
    record['snapshot']['adaptive']={'presented_question_ids':[shown['id']]}
    repo.put_record('assessments',record)
    state.send_message(sid,{'message':'Hello'})
    prompts=model.route_calls[0]['route_assessments'][0]['questions']
    assert prompts==[{'topic_id':shown['topic_id'],'prompt':shown['prompt']}]

def test_partial_rag_keeps_verified_grounding_and_generates_supplement(workspace):
    state,model,sid,_=setup(workspace,document=True,educational=True,uncertain=True)
    source=state.message_service._sources(state.get_space(sid))[0]
    class Pipeline:
        def answer(self,*a,**kw):
            return {'status':'partial','text':'Verified document fact.','sources':[source],
                    'citations':[{k:v for k,v in source.items() if k not in {'text','topic_name','topic_id'}}],'trace':{}}
    state.message_service.rag_pipeline=Pipeline()
    model.generate=lambda snap:{'text':'A separately labelled supplement.', 'citation_ids':[],
                                'supplement_text':'Additional model knowledge.'}
    response=state.send_message(sid,{'message':'Explain the document and wider implications'})
    run=state.get_run(response['run_id'])
    assert run['status']=='succeeded',run['error']
    done=next(e['data'] for e in run['events'] if e['event']=='message.completed')
    assert done['answer_mode']=='mixed' and done['answer_status']=='partial'
    assert 'Verified document fact.' in done['text'] and 'Additional model knowledge.' in done['text']
    assert done['citations'][0]['chunk_id']==source['chunk_id']


def test_delete_during_semantic_classification_cannot_publish(workspace):
    state,model,sid,_=setup(workspace)
    response=state.send_message(sid,{'message':'Hello'},dispatch=lambda *a:None)
    row=state.message_service.repository.records('messages',space_id=sid)[0]
    def classify(snapshot):
        state.delete_space(sid,{'confirm':True,'expected_version':state.get_space(sid)['space_version']})
        return model.route
    model.classify=classify
    state.message_service.generate(row['id'])
    assert state.message_service.repository.records('messages',space_id=sid)==[] and model.calls==[]

def test_mixed_stream_attribution_precedes_supplement_and_matches_final(workspace):
    state,model,sid,_=setup(workspace,document=True,educational=True,uncertain=True)
    source=state.message_service._sources(state.get_space(sid))[0]
    class Pipeline:
        def answer(self,*a,**kw):
            return {'status':'partial','text':'Verified document fact.','sources':[source],
                    'citations':[{k:v for k,v in source.items() if k not in {'text','topic_name','topic_id'}}],'trace':{}}
    state.message_service.rag_pipeline=Pipeline()
    model.stream_fields={'text','supplement_text'}
    def stream(snapshot,on_text,text_field):
        assert text_field=='supplement_text'
        events=state.run_service.events_for(current['run_id'])
        first=next(e['data']['delta'] for e in events if e['event']=='message.delta')
        assert first=='### 资料依据\n\nVerified document fact.\n\n### 通用知识补充\n\n'
        on_text('Additional ');on_text('model knowledge.')
        return {'text':'Badly formatted unlabelled text must never stream.', 'citation_ids':[],
                'supplement_text':'Additional model knowledge.'}
    model.stream=stream
    response=state.send_message(sid,{'message':'Explain document and supplement'},dispatch=lambda *a:None)
    current=state.message_service.repository.records('messages',space_id=sid)[0]
    state.message_service.generate(current['id'])
    run=state.get_run(response['run_id'])
    assert run['status']=='succeeded',run['error']
    done=next(e['data'] for e in run['events'] if e['event']=='message.completed')
    assert ''.join(e['data']['delta'] for e in run['events'] if e['event']=='message.delta')==done['text']
    route=next(e['data'] for e in run['events'] if e['event']=='message.route' and e['data']['answer_mode']=='mixed')
    assert route['supplement_explanation']


@pytest.mark.parametrize('educational',[False,True])
def test_unrelated_general_turn_does_not_audit_prior_material_history(workspace,educational):
    state,model,sid,_=setup(workspace,document=True,educational=True,uncertain=True)
    model.generate=lambda snap:{'text':'Document explanation.','citation_ids':[snap['sources'][0]['chunk_id']]}
    first=state.send_message(sid,{'message':'Explain the material'})
    assessment=create(state,sid)
    model.route=dict(document_related=False,educational=educational,topic_ids=[],scope_uncertain=False,reason='Clearly unrelated')
    model.generate=lambda snap:{'text':'A gardening explanation.' if educational else 'Hello!','citation_ids':[]}
    state.send_message(sid,{'message':'Explain gardening' if educational else 'Hello', 'session_id':first['session_id']})
    assert model.route_calls[-1]['history'] # semantic router can resolve what the user is referring to
    record=state.message_service.repository.records('messages',space_id=sid)[-1]
    assert record['snapshot']['history']==[] and record['snapshot']['delivered_source_refs']==[]
    assert not any(q.get('assisted') for q in state.assessment_service.repository.get_record('assessments',assessment['id'])['questions'])

def test_original_material_mixed_stream_uses_sections_and_never_raw_text(workspace):
    state,model,sid,_=setup(workspace,document=True,educational=True,uncertain=True)
    model.section_streaming=True
    def stream(snapshot,on_text,on_section):
        assert on_text is None
        on_section('grounded_text','Document fact.')
        events=state.run_service.events_for(current['run_id'])
        first=next(e['data']['delta'] for e in events if e['event']=='message.delta')
        assert first.startswith('### 资料依据\n\n')
        on_section('supplement_text','General context.')
        return {'text':'Unattributed raw text must not stream.','grounded_text':'Document fact.',
                'supplement_text':'General context.','citation_ids':[snapshot['sources'][0]['chunk_id']]}
    model.stream=stream
    response=state.send_message(sid,{'message':'Explain the current material'},dispatch=lambda *a:None)
    current=state.message_service.repository.records('messages',space_id=sid)[0]
    state.message_service.generate(current['id'])
    run=state.get_run(response['run_id'])
    assert run['status']=='succeeded',run['error']
    done=next(e['data'] for e in run['events'] if e['event']=='message.completed')
    assert done['answer_mode']=='mixed'
    assert ''.join(e['data']['delta'] for e in run['events'] if e['event']=='message.delta')==done['text']

@pytest.mark.parametrize('reverse',[False,True])
def test_configured_material_section_stream_accepts_json_key_order(workspace,monkeypatch,reverse):
    import json
    import knowpath_backend.learning.providers.models as providers
    from knowpath_backend.learning.conversations.generation import DashScopeAnswerGenerator
    state,model,sid,_=setup(workspace,document=True,educational=True,uncertain=True)
    class Provider:
        def stream(self,messages,**kwargs):
            snapshot=json.loads(messages[-1]['content'])
            sections=[('supplement_text','Model context.'),('grounded_text','Document fact.')]
            if not reverse:sections.reverse()
            raw=dict(sections,text='Never stream this unlabelled text.',citation_ids=[snapshot['sources'][0]['chunk_id']])
            for char in json.dumps(raw,ensure_ascii=False):yield char
    monkeypatch.setattr(providers,'chat_model',lambda settings,client=None:Provider())
    generator=DashScopeAnswerGenerator(state.message_service.context_settings)
    generator.classify=model.classify
    state.message_service.generator=generator
    response=state.send_message(sid,{'message':'Explain the material plus context'})
    run=state.get_run(response['run_id'])
    assert run['status']=='succeeded',run['error']
    done=next(e['data'] for e in run['events'] if e['event']=='message.completed')
    deltas=[e['data']['delta'] for e in run['events'] if e['event']=='message.delta']
    assert deltas[0]=='### 资料依据\n\n'
    assert ''.join(deltas)==done['text'] and done['answer_mode']=='mixed'
