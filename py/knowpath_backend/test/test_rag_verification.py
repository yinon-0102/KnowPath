"""Only supported claims survive, including after the one permitted revision."""
import copy
import json
import time

import pytest

from knowpath_backend.learning.rag.verification import AnswerVerifier, VerificationError


SOURCES = [dict(chunk_id="c1", source_text="只有完成培训才能申请。", source_spans=[])]


def answer(text="完成培训后可申请。", *, status="answered"):
    return dict(status=status, claims=[dict(claim_id="claim1", text=text, kind="fact",
        citation_ids=["c1"], depends_on=[])], missing_points=[])


def verdict(status="supported", complete=True):
    return dict(checks=[dict(claim_id="claim1", status=status, citation_ids=["c1"], reason="原文核对")],
                missing_points=[], complete=complete)


class Model:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
    def generate_json(self, messages, *, deadline):
        self.calls.append(copy.deepcopy(messages))
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_revision_is_verified_again_before_publication():
    generator = Model([answer("所有人都能申请。"), answer()])
    checker = Model([verdict("unsupported"), verdict()])
    result = AnswerVerifier(generator, checker).answer("谁能申请？", SOURCES, deadline=time.monotonic() + 10)
    assert result["status"] == "answered" and result["text"] == "完成培训后可申请。"
    assert len(generator.calls) == 2 and len(checker.calls) == 2
    assert result["trace"]["revisions"] == 1


def test_second_failure_is_removed_without_third_model_call():
    generator = Model([answer("无需培训。"), answer("没有条件。")])
    checker = Model([verdict("unsupported"), verdict("undetermined")])
    result = AnswerVerifier(generator, checker).answer("谁能申请？", SOURCES, deadline=time.monotonic() + 10)
    assert result["status"] == "insufficient" and result["citation_ids"] == []
    assert "没有条件" not in result["text"]
    assert len(generator.calls) == len(checker.calls) == 2


@pytest.mark.parametrize("bad", [{}, verdict("unknown")])
def test_verifier_failure_is_not_evidence_insufficiency(bad):
    with pytest.raises(VerificationError) as error:
        AnswerVerifier(Model([answer()]), Model([bad])).answer("条件？", SOURCES, deadline=time.monotonic() + 10)
    assert error.value.code == "VERIFICATION_UNAVAILABLE"
    assert "secret" not in str(error.value)


def test_no_evidence_returns_insufficient_without_model_calls():
    generator, checker = Model([]), Model([])
    result = AnswerVerifier(generator, checker).answer("条件？", [], deadline=time.monotonic() + 10)
    assert result["status"] == "insufficient"
    assert generator.calls == checker.calls == []


def test_supported_subset_cannot_publish_an_unverified_citation():
    draft = answer()
    draft['claims'][0]['citation_ids'].append('wrong')
    with pytest.raises(VerificationError):
        AnswerVerifier(Model([draft]), Model([verdict()])).answer('条件？',
            SOURCES + [dict(chunk_id='wrong', source_text='无关原文')], deadline=time.monotonic() + 10)


def test_one_call_budget_prunes_without_revision():
    generator, checker = Model([answer()]), Model([verdict('unsupported')])
    result = AnswerVerifier(generator, checker).answer('条件？', SOURCES, deadline=time.monotonic() + 10,
        max_generation_calls=1, max_verification_calls=1)
    assert result['status'] == 'insufficient'
    assert len(generator.calls) == len(checker.calls) == 1
    assert result['trace']['revisions'] == 0


def test_fact_numeric_mismatch_cannot_be_overruled_by_model_support():
    generator = Model([answer('有效期为99天。'), answer('有效期为99天。')])
    checker = Model([verdict(), verdict()])
    result = AnswerVerifier(generator,checker).answer('有效期多久？',
        [dict(chunk_id='c1',source_text='有效期为30天。')],deadline=time.monotonic()+10)
    assert result['status']=='insufficient'
    assert result['trace']['verdicts'][-1]['checks'][0]['status']=='unsupported'


@pytest.mark.parametrize('source,text',[('期限十五日。','期限15日。'),('比例0.50。','比例0.5。'),('二〇二四年。','2024年。')])
def test_equivalent_numeric_formats_are_not_rejected(source,text):
    result=AnswerVerifier(Model([answer(text)]),Model([verdict()])).answer('请说明。',
        [dict(chunk_id='c1',source_text=source)],deadline=time.monotonic()+10)
    assert result['status']=='answered'


def test_claim_cannot_use_unknown_or_empty_citations():
    invalid = answer()
    invalid["claims"][0]["citation_ids"] = ["outside"]
    with pytest.raises(VerificationError):
        AnswerVerifier(Model([invalid]), Model([])).answer("条件？", SOURCES, deadline=time.monotonic() + 10)


def test_chinese_ordinary_words_are_not_numeric_claims():
    result = AnswerVerifier(Model([answer('一般应当先完成培训。')]), Model([verdict()])).answer('条件？',
        [dict(chunk_id='c1', source_text='应当先完成培训。')], deadline=time.monotonic()+10)
    assert result['status'] == 'answered'


def test_unsupported_premise_removes_dependent_claim():
    raw = answer()
    raw["claims"].append(dict(claim_id="derived", text="推导结论", kind="inference", citation_ids=["c1"], depends_on=["claim1"]))
    checked = verdict("unsupported")
    checked["checks"].append(dict(claim_id="derived", status="supported", citation_ids=["c1"], reason="conditional"))
    result = AnswerVerifier(Model([raw, raw]), Model([checked, checked])).answer("推导？", SOURCES, deadline=time.monotonic() + 10)
    assert result["status"] == "insufficient" and "推导结论" not in result["text"]


def test_deadline_prevents_any_call():
    generator, checker = Model([]), Model([])
    with pytest.raises(VerificationError) as error:
        AnswerVerifier(generator, checker).answer("问题", SOURCES, deadline=time.monotonic() - 1)
    assert error.value.code == "RAG_DEADLINE_EXCEEDED"
    assert generator.calls == checker.calls == []


def point(point_id='p1', text='申请条件'):
    return dict(point_id=point_id, text=text)


def point_check(point_id='p1', text='申请条件', status='supported', claim_ids=None):
    return dict(**point(point_id, text), status=status, citation_ids=['c1'],
                claim_ids=['claim1'] if claim_ids is None else claim_ids, reason='逐要点核对')


def test_checker_discovered_omitted_requirement_blocks_complete_answer():
    draft = dict(answer(), required_points=[point()])
    checked = dict(verdict(), requirement_checks=[point_check(),
        point_check('p2', '申请期限', 'undetermined', [])])
    result = AnswerVerifier(Model([draft]), Model([checked])).answer('条件和期限？', SOURCES,
        deadline=time.monotonic()+10, max_generation_calls=1)
    assert result['status'] == 'partial'
    assert result['trace']['verdicts'][0]['requirement_checks'][1]['point_id'] == 'p2'


def test_revision_cannot_drop_checker_discovered_requirement():
    draft = dict(answer(), required_points=[point()])
    checked = dict(verdict(), requirement_checks=[point_check(),
        point_check('p2', '申请期限', 'undetermined', [])])
    with pytest.raises(VerificationError, match='GENERATION_INVALID_RESPONSE'):
        AnswerVerifier(Model([draft, draft]), Model([checked])).answer('条件和期限？', SOURCES,
            deadline=time.monotonic()+10)


@pytest.mark.parametrize('checks', [[], [point_check('wrong')], [point_check(), point_check()],
    [dict(point_check(), citation_ids=['unknown'])], [dict(point_check(), claim_ids=[])],
    [dict(point_check(), text='不同要求')], [dict(point_check(), claim_ids=['unknown'])]])
def test_requirement_checks_reject_missing_duplicate_changed_or_invalid_references(checks):
    draft = dict(answer(), required_points=[point()])
    with pytest.raises(VerificationError, match='VERIFICATION_UNAVAILABLE'):
        AnswerVerifier(Model([draft]), Model([dict(verdict(), requirement_checks=checks)])).answer(
            '条件？', SOURCES, deadline=time.monotonic()+10)


def test_numeric_rejection_also_invalidates_requirement_support():
    draft = dict(answer('期限99天。'), required_points=[point('p1','申请期限')])
    checked = dict(verdict(), requirement_checks=[point_check('p1','申请期限')])
    result = AnswerVerifier(Model([draft]), Model([checked])).answer('期限？',
        [dict(chunk_id='c1',source_text='期限30天。')], deadline=time.monotonic()+10,
        max_generation_calls=1)
    assert result['status'] == 'insufficient'
    assert result['trace']['verdicts'][0]['requirement_checks'][0]['status'] == 'unsupported'


def test_no_claims_still_checks_structured_requirements():
    draft = dict(status='insufficient', claims=[], missing_points=['申请期限'], required_points=[point()])
    checked = dict(checks=[], missing_points=['申请期限'], complete=False,
                   requirement_checks=[point_check(status='undetermined', claim_ids=[])])
    checker = Model([checked])
    result = AnswerVerifier(Model([draft]), checker).answer('期限？', SOURCES,
        deadline=time.monotonic()+10, max_generation_calls=1)
    assert result['status'] == 'insufficient'
    assert len(checker.calls) == 1


def test_real_adapter_requires_checklist_but_legacy_injection_remains_compatible():
    generator = Model([answer()])
    generator.requires_required_points = True
    with pytest.raises(VerificationError, match='GENERATION_INVALID_RESPONSE'):
        AnswerVerifier(generator, Model([])).answer('条件？', SOURCES, deadline=time.monotonic()+10)


def test_required_point_successful_revision_keeps_identity_and_uses_two_calls():
    first = dict(answer(), required_points=[point()])
    second = copy.deepcopy(first)
    second['required_points'].append(point('p2', '例外条件'))
    second['claims'].append(dict(claim_id='claim2', text='存在例外。', kind='fact', citation_ids=['c1']))
    initial = dict(verdict(), requirement_checks=[point_check(), point_check('p2', '例外条件', 'unsupported', [])])
    final = dict(verdict(), requirement_checks=[point_check(), point_check('p2','例外条件',claim_ids=['claim2'])])
    final['checks'].append(dict(claim_id='claim2', status='supported', citation_ids=['c1'], reason='例外核对'))
    generator, checker = Model([first,second]), Model([initial,final])
    result = AnswerVerifier(generator,checker).answer('条件及例外？', SOURCES, deadline=time.monotonic()+10)
    assert result['status'] == 'answered'
    assert len(generator.calls) == len(checker.calls) == 2
    assert [p['point_id'] for p in result['trace']['verdicts'][-1]['requirement_checks']] == ['p1','p2']


def test_checklist_cannot_disappear_at_verification_stage():
    with pytest.raises(VerificationError, match='VERIFICATION_UNAVAILABLE'):
        AnswerVerifier(Model([dict(answer(), required_points=[point()])]), Model([verdict()])).answer(
            '条件？', SOURCES, deadline=time.monotonic()+10)


def test_unsupported_premise_invalidates_dependent_required_point():
    draft = dict(answer(),required_points=[point()])
    draft['claims'].append(dict(claim_id='derived', text='推导结论', kind='inference',
        citation_ids=['c1'], depends_on=['claim1']))
    checked = dict(verdict('unsupported'), requirement_checks=[point_check(claim_ids=['derived'])])
    checked['checks'].append(dict(claim_id='derived', status='supported', citation_ids=['c1'],reason='前提依赖'))
    result = AnswerVerifier(Model([draft]),Model([checked])).answer('条件？',SOURCES,
        deadline=time.monotonic()+10,max_generation_calls=1)
    assert result['status']=='insufficient'
    assert result['trace']['verdicts'][0]['requirement_checks'][0]['status']=='unsupported'


def test_repair_payload_keeps_evidence_but_deduplicates_requirement_text():
    first = dict(answer(), required_points=[point()])
    second = dict(answer(), required_points=[point(),point('p2','申请期限')])
    initial = dict(verdict(), requirement_checks=[point_check(),point_check('p2','申请期限','undetermined',[])])
    final = dict(verdict(), requirement_checks=[point_check(),point_check('p2','申请期限')])
    generator,checker = Model([first,second]),Model([initial,final])
    result = AnswerVerifier(generator,checker).answer('条件和期限？',SOURCES,deadline=time.monotonic()+10)
    payloads = [json.loads(call[1]['content']) for call in
                (generator.calls[0],checker.calls[0],generator.calls[1],checker.calls[1])]
    assert all(payload['evidence']==payloads[0]['evidence'] for payload in payloads)
    repair = payloads[2]
    assert repair['required_points']==second['required_points']
    assert 'required_points' not in repair['previous_draft']
    assert all('text' not in p for p in repair['verification_feedback']['requirement_checks'])
    failed = repair['verification_feedback']['requirement_checks'][1]
    assert failed == {key:value for key,value in initial['requirement_checks'][1].items() if key!='text'}
    assert repair['verification_feedback']['checks'][0]=={'claim_id':'claim1','status':'supported'}
    assert set(payloads[3]) == {'question','evidence','draft'}
    assert payloads[3]['draft']['required_points']==second['required_points']
    assert result['status']=='answered'


def test_real_adapters_fit_four_calls_without_old_draft_in_second_check(monkeypatch):
    import httpx
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.rag.model_services import BudgetedJsonModel, _encoded_size
    from knowpath_backend.learning.rag.verification import GENERATION_SYSTEM, VERIFICATION_SYSTEM
    monkeypatch.setenv('CHECKLIST_TEST_KEY','test-only')
    settings = LearningSettings(chat_api_key_env='CHECKLIST_TEST_KEY')
    draft = dict(answer('Evidence fact. '*110),required_points=[point('p1','State the fact')])
    initial = dict(verdict(), complete=False, requirement_checks=[point_check('p1','State the fact')])
    initial['checks'][0]['reason'] = 'Check explanation. '*70
    initial['requirement_checks'][0]['reason'] = 'Requirement explanation. '*55
    final = dict(verdict(),requirement_checks=[point_check('p1','State the fact')])
    outputs = iter([draft,initial,draft,final])
    bodies=[]
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{
            'role':'assistant','content':json.dumps(next(outputs))}}],
            'usage':{'prompt_tokens':100,'completion_tokens':100}})
    source = [dict(chunk_id='c1',source_text='Evidence fact. '*350)]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        generator,checker = BudgetedJsonModel(settings,client=client),BudgetedJsonModel(settings,client=client)
        # This historical four-call envelope fixture intentionally exercises
        # the legacy protocol; V2 wire/schema behavior has separate coverage.
        generator.protocol_version = checker.protocol_version = 1
        result=AnswerVerifier(generator,checker).answer('State the fact',source,deadline=time.monotonic()+10)
    assert result['status']=='answered' and len(bodies)==4
    assert all(_encoded_size(body)<=12000 for body in bodies)
    # Reconstruct the pre-fix second-check envelope: it redundantly included
    # the first draft and verdict on top of the newly generated draft.
    old_user = dict(json.loads(bodies[0]['messages'][1]['content']), previous_draft=draft,
                    verification_feedback=initial, draft=draft,
                    instruction='Only one repair allowed.')
    old_body = {**bodies[3], 'messages':[{'role':'system','content':VERIFICATION_SYSTEM},
        {'role':'user','content':json.dumps(old_user,ensure_ascii=False)}]}
    assert _encoded_size(old_body)>12000
    old_user.pop('draft')
    old_repair_body = {**bodies[2], 'messages':[{'role':'system','content':GENERATION_SYSTEM},
        {'role':'user','content':json.dumps(old_user,ensure_ascii=False)}]}
    assert _encoded_size(old_repair_body)>12000


@pytest.mark.parametrize('kind,label',[('inference','【推导】'),('example','【示例】'),('fact','')])
def test_verified_kind_has_deterministic_visible_label_without_changing_claim_text(kind,label):
    draft = answer('完成培训后可申请。')
    draft['claims'][0]['kind']=kind
    result=AnswerVerifier(Model([draft]),Model([verdict()])).answer('条件？',SOURCES,deadline=time.monotonic()+10)
    assert result['text']==label+draft['claims'][0]['text']
    assert result['claims'][0]['text']==draft['claims'][0]['text']
    assert result['trace']['drafts'][0]['claims'][0]['text']==draft['claims'][0]['text']
