"""Versioned, bounded verifier protocol and explicit repair decisions."""
import copy
import json
import time

import pytest

from knowpath_backend.learning.rag.verification import AnswerVerifier, VerificationError


SOURCE = {'chunk_id': 'immutable-material-version-chunk', 'source_text': '培训合格且年满十八岁才可申请，紧急情形除外。'}


class Model:
    protocol_version = 2

    def __init__(self, outputs):
        self.outputs, self.calls, self.schemas = list(outputs), [], []

    def generate_json(self, messages, *, deadline, response_schema=None):
        self.calls.append(copy.deepcopy(messages))
        self.schemas.append(response_schema)
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def draft(status='answered', text=None):
    return dict(status=status, claims=[] if status in {'insufficient', 'clarify'} else [dict(
        claim_id='c1', text=text or SOURCE['source_text'], kind='fact', citation_ids=['s1'], depends_on=[])],
        required_points=[dict(point_id='p1', text='必要申请条件及例外')], missing_points=[])


def verdict(reason='complete', issue='none'):
    nonanswer = reason in {'evidence_missing', 'clarification_needed'}
    return dict(reason_code=reason, complete=reason == 'complete', missing_points=[],
        checks=[] if nonanswer else [dict(claim_id='c1', status='supported', citation_ids=['s1'],
            issue_type=issue, qualifier_checks={key:'preserved' for key in
                ('subject', 'conditions', 'exceptions', 'negation', 'quantifiers')},
            evidence_spans=[dict(evidence_id='e1')])],
        requirement_checks=[dict(point_id='p1', status='undetermined' if nonanswer else 'supported',
            citation_ids=[] if nonanswer else ['s1'], claim_ids=[] if nonanswer else ['c1'])])


def run(generator, checker, **kwargs):
    return AnswerVerifier(generator, checker, **kwargs).answer('申请有哪些条件？', [SOURCE], deadline=time.monotonic()+30)


@pytest.mark.parametrize('reason,status', [('evidence_missing','insufficient'), ('clarification_needed','clarify')])
def test_confirmed_nonanswer_stops_after_first_valid_verification(reason, status):
    generator, checker = Model([draft(status)]), Model([verdict(reason)])
    result = run(generator, checker)
    assert result['status'] == status
    assert len(generator.calls) == len(checker.calls) == 1
    assert result['trace']['revisions'] == 0


def test_generator_nonanswer_still_requires_valid_checker():
    generator, checker = Model([draft('insufficient')]), Model([{}])
    with pytest.raises(VerificationError, match='VERIFICATION_UNAVAILABLE'):
        run(generator, checker)
    assert len(checker.calls) == 1


def test_terminal_generator_exception_keeps_provider_error_when_retry_is_enabled():
    generator, checker = Model([RuntimeError('transport failed'), RuntimeError('transport failed')]), Model([])
    generator.allows_contract_retry = True
    with pytest.raises(VerificationError, match='GENERATION_INVALID_RESPONSE') as error:
        run(generator, checker)
    assert error.value.details['failure_kind'] == 'response_schema'
    assert len(generator.calls) == 2
    assert checker.calls == []


def test_wire_ids_restore_exact_identity_and_checklist_text_is_not_repeated():
    generator, checker = Model([draft()]), Model([verdict()])
    result = run(generator, checker)
    assert result['citation_ids'] == [SOURCE['chunk_id']]
    assert result['trace']['verdicts'][0]['checks'][0]['evidence_spans'][0]['citation_id'] == SOURCE['chunk_id']
    for model in (generator, checker):
        payload = json.loads(model.calls[0][1]['content'])
        assert payload['evidence'][0]['chunk_id'] == 's1'
        assert SOURCE['chunk_id'] not in model.calls[0][1]['content']
        assert model.schemas[0] is not None
    assert checker.calls[0][1]['content'].count('必要申请条件及例外') == 1


@pytest.mark.parametrize('field', ['subject','conditions','exceptions','negation','quantifiers'])
def test_failed_qualifier_cannot_publish_supported_claim(field):
    checked = verdict('evidence_missing')
    checked['checks'] = verdict()['checks']
    checked['checks'][0]['qualifier_checks'][field] = 'violated'
    checked['checks'][0]['issue_type'] = 'qualifier_missing'
    result = run(Model([draft(text='培训合格就可以申请。')]), Model([checked]))
    assert result['status'] == 'insufficient'
    assert result['claims'] == []


@pytest.mark.parametrize('mutation', ['foreign_id','span_out_of_bounds','missing_qualifier','missing_reason_code','missing_issue'])
def test_v2_invalid_contract_is_service_failure(mutation):
    checked = verdict()
    if mutation == 'foreign_id': checked['checks'][0]['citation_ids'] = [SOURCE['chunk_id']]
    if mutation == 'span_out_of_bounds': checked['checks'][0]['evidence_spans'][0]['end'] = 10000
    if mutation == 'missing_qualifier': del checked['checks'][0]['qualifier_checks']['conditions']
    if mutation == 'missing_reason_code': del checked['reason_code']
    if mutation == 'missing_issue': del checked['checks'][0]['issue_type']
    with pytest.raises(VerificationError, match='VERIFICATION_UNAVAILABLE'):
        run(Model([draft()]), Model([checked]))


def test_repair_admission_rejects_before_second_generation_and_never_returns_insufficient():
    generator, checker = Model([draft(), draft()]), Model([verdict('answer_incomplete')])
    def reject(*args, **kwargs):
        raise VerificationError('MODEL_TOKEN_BUDGET_EXCEEDED')
    with pytest.raises(VerificationError, match='MODEL_TOKEN_BUDGET_EXCEEDED'):
        run(generator, checker, revision_admission=reject)
    assert len(generator.calls) == len(checker.calls) == 1


def test_only_repairable_incomplete_answer_gets_one_fully_checked_revision():
    generator, checker = Model([draft(), draft()]), Model([verdict('answer_incomplete'), verdict()])
    admitted = []
    result = run(generator, checker, revision_admission=lambda *args, **kwargs: admitted.append(args))
    assert result['status'] == 'answered'
    assert len(generator.calls) == len(checker.calls) == 2
    assert len(admitted) == 1


def test_oversized_protocol_returns_explicit_failure_without_truncating_evidence():
    raw = draft()
    raw['claims'][0]['text'] = 'x' * 1201
    with pytest.raises(VerificationError, match='GENERATION_INVALID_RESPONSE'):
        run(Model([raw]), Model([]))


def test_unresolved_second_draft_never_causes_a_third_call():
    generator = Model([draft(), draft()])
    checker = Model([verdict('answer_incomplete'), verdict('answer_incomplete')])
    result = run(generator, checker)
    assert result['status'] == 'partial'
    assert len(generator.calls) == len(checker.calls) == 2


def test_failed_second_checker_never_publishes_new_draft():
    generator = Model([draft(), draft(text='未经核验的新内容')])
    checker = Model([verdict('answer_incomplete'), RuntimeError('private service payload')])
    with pytest.raises(VerificationError, match='VERIFICATION_UNAVAILABLE') as error:
        run(generator, checker)
    assert 'private' not in str(error.value)


def test_real_adapter_retries_one_rejected_generation_contract_with_budgeted_pair():
    bad = draft('insufficient')
    bad['claims'] = [dict(claim_id='c1', text='不应出现在拒答中的内容', kind='fact',
                          citation_ids=['s1'], depends_on=[])]
    generator = Model([bad, draft('insufficient')])
    generator.allows_contract_retry = True
    checker = Model([verdict('evidence_missing')])
    result = run(generator, checker)
    assert result['status'] == 'insufficient'
    assert len(generator.calls) == 2
    assert len(checker.calls) == 1
    assert json.loads(generator.calls[1][1]['content'])['previous_output_rejected'] is True


def test_real_adapter_retries_one_output_capacity_rejection():
    generator = Model([VerificationError('MODEL_OUTPUT_CAPACITY_EXCEEDED',
        details={'failure_kind':'output_budget'}), draft()])
    generator.allows_contract_retry = True
    result = run(generator, Model([verdict()]))
    assert result['status'] == 'answered'
    assert len(generator.calls) == 2


def test_terminal_nonanswer_contract_is_safely_normalized_to_empty_answer():
    bad = draft('insufficient')
    bad['claims'] = [dict(claim_id='c1', text='不应发布', kind='fact',
                          citation_ids=['s1'], depends_on=[])]
    generator = Model([bad, bad])
    generator.allows_contract_retry = True
    checker = Model([verdict('evidence_missing')])
    result = run(generator, checker)
    assert result['status'] == 'insufficient'
    assert result['claims'] == []
    assert result['trace']['contract_repairs'] == [{'stage':'generation', 'kind':'drop_nonanswer_claims'}]


def test_real_adapter_retries_one_rejected_checker_contract_without_publishing_unchecked_text():
    generator = Model([draft(), draft()])
    generator.allows_contract_retry = True
    bad = verdict()
    bad['checks'][0]['evidence_spans'] = [dict(evidence_id='e2')]
    checker = Model([bad, verdict()])
    checker.allows_contract_retry = True
    result = run(generator, checker)
    assert result['status'] == 'answered'
    assert len(generator.calls) == len(checker.calls) == 2
    assert json.loads(generator.calls[1][1]['content'])['previous_output_rejected'] is True


def test_v2_numeric_rejection_invalidates_point_and_preserves_exact_sources():
    generator, checker = Model([draft(text='年满99岁才可申请。')]), Model([verdict()])
    result = AnswerVerifier(generator, checker).answer('申请条件？', [SOURCE],
        deadline=time.monotonic()+30, max_generation_calls=1)
    assert result['status'] == 'insufficient'
    assert result['claims'] == []
    assert ''.join(s['text'] for s in json.loads(checker.calls[0][1]['content'])['evidence'][0]['segments']) == SOURCE['source_text']


def test_v2_cross_page_evidence_and_unsupported_dependency_are_checked():
    raw = draft()
    raw['claims'].append(dict(claim_id='c2', text='推导可申请。', kind='inference',
        citation_ids=['s1','s2'], depends_on=['c1']))
    checked = verdict('evidence_missing')
    checked['checks'] = verdict()['checks']
    checked['checks'][0]['status'] = 'unsupported'
    checked['checks'][0]['issue_type'] = 'qualifier_missing'
    checked['checks'].append(dict(checked['checks'][0], claim_id='c2', status='supported', issue_type='none',
        citation_ids=['s1','s2'], evidence_spans=[dict(evidence_id='e1'), dict(evidence_id='e2')]))
    result = AnswerVerifier(Model([raw]),Model([checked])).answer('申请条件？',
        [SOURCE, dict(chunk_id='next-page-immutable', source_text='续页例外。')], deadline=time.monotonic()+30)
    assert result['claims'] == []


def test_checker_can_add_required_point_without_echoing_original_text():
    initial = verdict('answer_incomplete')
    initial['requirement_checks'].append(dict(point_id='p2', text='例外主体范围',
        status='unsupported', citation_ids=[], claim_ids=[], reason='原文存在必要限定'))
    revised = draft()
    revised['required_points'].append(dict(point_id='p2', text='例外主体范围'))
    final = verdict()
    final['requirement_checks'].append(dict(final['requirement_checks'][0],point_id='p2'))
    generator, checker = Model([draft(), revised]), Model([initial, final])
    result = run(generator, checker)
    assert result['status'] == 'answered'
    assert generator.calls[1][1]['content'].count('例外主体范围') == 1


def test_supported_fact_cannot_silently_change_citation_association():
    checked = verdict()
    checked['checks'][0]['citation_ids'] = ['s2']
    checked['checks'][0]['evidence_spans'][0]['evidence_id'] = 'e2'
    with pytest.raises(VerificationError, match='VERIFICATION_UNAVAILABLE'):
        AnswerVerifier(Model([draft()]),Model([checked])).answer('申请条件？',
            [SOURCE, dict(SOURCE, chunk_id='other')], deadline=time.monotonic()+30)


def test_checker_source_ids_are_derived_from_evidence_spans():
    """A valid evidence anchor is authoritative for its source association."""
    checked = verdict()
    # Simulate the provider confusing a source short ID while selecting the
    # correct evidence segment. Local protocol code must derive s1 from e1.
    checked['checks'][0]['citation_ids'] = ['s2']
    result = AnswerVerifier(Model([draft()]), Model([checked])).answer(
        '申请条件？', [SOURCE, dict(SOURCE, chunk_id='other')],
        deadline=time.monotonic()+30, max_generation_calls=1)
    assert result['status'] == 'answered'
    assert tuple(result['trace']['verdicts'][0]['checks'][0]['citation_ids']) == (SOURCE['chunk_id'],)


def test_revision_preserves_original_required_point_text_when_model_rephrases_it():
    first = draft()
    revised = draft(text='补充完整条件。')
    revised['required_points'] = [dict(point_id='p1', text='模型改写的要点')]
    result = run(Model([first, revised]), Model([verdict('answer_incomplete'), verdict()]))
    assert result['status'] == 'answered'
    assert result['trace']['drafts'][1]['required_points'][0]['text'] == first['required_points'][0]['text']


def test_json_object_fallback_prompts_contain_complete_parseable_examples():
    generator, checker = Model([draft()]), Model([verdict()])
    run(generator, checker)
    generated = json.loads(generator.calls[0][0]['content'].split('\nJSON示例：')[1])
    checked = json.loads(checker.calls[0][0]['content'].split('\nJSON示例：')[1])
    assert set(generated) == set(draft())
    assert set(generated['claims'][0]) == set(draft()['claims'][0])
    assert set(checked) == set(verdict())
    assert set(checked['checks'][0]) >= set(verdict()['checks'][0])


def test_explicit_wire_byte_capacity_rejects_draft_without_truncation_or_check_call():
    generator, checker = Model([draft()]), Model([])
    generator.max_draft_bytes = 100
    with pytest.raises(VerificationError, match='MODEL_OUTPUT_CAPACITY_EXCEEDED'):
        run(generator, checker)
    assert checker.calls == []


def test_initial_packing_templates_use_actual_wire_prompts_and_complete_evidence():
    generator, checker = Model([draft()]), Model([verdict()])
    verifier = AnswerVerifier(generator, checker)
    first, check_template = verifier.initial_messages('申请有哪些条件？', [SOURCE])
    run(generator, checker)
    assert first == generator.calls[0]
    check_data = json.loads(check_template[1]['content'])
    assert check_template[0] == checker.calls[0][0]
    assert check_data['draft'] == {}
    assert ''.join(s['text'] for s in check_data['evidence'][0]['segments']) == SOURCE['source_text']


def test_exhausted_incomplete_answer_without_supported_claims_is_safe_insufficient_result():
    checked = verdict('answer_incomplete')
    checked['checks'][0]['status'] = 'unsupported'
    checked['checks'][0]['issue_type'] = 'answer_incomplete'
    generator, checker = Model([draft(), draft()]), Model([checked, checked])
    result = run(generator, checker)
    assert result['status'] == 'insufficient'
    assert result['claims'] == []
    assert len(generator.calls) == len(checker.calls) == 2


@pytest.mark.parametrize('failure', ['numeric', 'qualifier'])
def test_locally_rejected_complete_verdict_gets_bounded_repair(failure):
    first = draft(text='年满99岁才可申请。') if failure == 'numeric' else draft()
    checked = verdict()
    if failure == 'qualifier': checked['checks'][0]['qualifier_checks']['conditions'] = 'violated'
    generator, checker = Model([first, draft()]), Model([checked, verdict()])
    result = run(generator, checker)
    assert result['status'] == 'answered'
    assert len(generator.calls) == len(checker.calls) == 2
    initial = result['trace']['verdicts'][0]
    assert initial['reason_code'] == 'answer_incomplete'
    assert initial['complete'] is False
    assert initial['requirement_checks'][0]['status'] == 'unsupported'


def test_incomplete_partial_does_not_claim_evidence_is_missing():
    result = run(Model([draft(), draft()]), Model([verdict('answer_incomplete'), verdict('answer_incomplete')]))
    assert result['status'] == 'partial'
    assert result['text'].startswith(SOURCE['source_text'])
    assert result['text'].endswith('其余部分尚未形成通过核验的回答。')
    assert '证据不足' not in result['text']


def test_clarification_partial_asks_for_scope_instead_of_claiming_missing_evidence():
    checked = verdict()
    checked.update(reason_code='clarification_needed', complete=False)
    result = run(Model([draft()]), Model([checked]))
    assert result['status'] == 'partial'
    assert result['text'].endswith('其余部分需要明确问题对象或资料范围。')
    assert '证据不足' not in result['text']


def test_checker_added_checklist_that_cannot_fit_draft_fails_before_revision():
    first = draft()
    generator, checker = Model([first]), Model([verdict('answer_incomplete')])
    generator.max_draft_bytes = len(json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 20
    checker.outputs[0]['requirement_checks'].append(dict(point_id='p2', text='必要条件'*40,
        status='unsupported', citation_ids=[], claim_ids=[]))
    with pytest.raises(VerificationError, match='MODEL_OUTPUT_CAPACITY_EXCEEDED'):
        run(generator, checker)
    assert len(generator.calls) == len(checker.calls) == 1


def test_draft_capacity_includes_normalized_optional_defaults():
    raw = draft()
    del raw['claims'][0]['depends_on']
    generator, checker = Model([raw]), Model([])
    generator.max_draft_bytes = len(json.dumps(raw, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    with pytest.raises(VerificationError, match='MODEL_OUTPUT_CAPACITY_EXCEEDED'):
        run(generator, checker)
    assert checker.calls == []


def test_numeric_fact_must_be_in_checked_span_not_elsewhere_in_same_chunk():
    source = dict(SOURCE, source_text='age18. unrelated99.')
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = [dict(evidence_id='e1')]
    generator, checker = Model([draft(text='age99.')]), Model([checked])
    result = AnswerVerifier(generator, checker).answer('age?', [source], deadline=time.monotonic()+30,
        max_generation_calls=1)
    assert result['status'] == 'insufficient'
    assert result['claims'] == []


@pytest.mark.parametrize('text,segment_id,start,end', [
    ('学校应当告知监护人。', 'e1', 0, 10),
    ('😀提示。学校应当告知监护人。🧑‍🎓', 'e2', 4, 14),
    ('前文。👩🏽‍🎓完成培训。后文。', 'e2', 3, 12),
    ('重复。重复。', 'e2', 3, 6)])
def test_segment_id_resolves_unicode_offsets_locally(text, segment_id, start, end):
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = [dict(evidence_id=segment_id)]
    result = AnswerVerifier(Model([draft(text=text[start:end])]), Model([checked])).answer(
        '说明？', [dict(SOURCE,source_text=text)], deadline=time.monotonic()+30)
    span = result['trace']['verdicts'][0]['checks'][0]['evidence_spans'][0]
    assert span == dict(citation_id=SOURCE['chunk_id'], start=start, end=end)


@pytest.mark.parametrize('bad_span', [dict(evidence_id='private-missing-id'),
    dict(citation_id='s1',quote='学校应当告知监护人。'), dict(citation_id='s1',start=0,end=12)])
def test_unknown_anchor_or_model_text_offsets_are_rejected(bad_span):
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = [bad_span]
    with pytest.raises(VerificationError, match='VERIFICATION_UNAVAILABLE') as error:
        run(Model([draft()]),Model([checked]))
    assert error.value.details['schema_rule'] == ('span_not_found' if 'evidence_id' in bad_span else 'schema_fields')
    assert 'private' not in json.dumps(error.value.details)


@pytest.mark.parametrize('mutation,rule', [('unknown','citation_identity'),
    ('required','required_points'), ('missing','schema_fields')])
def test_v2_contract_failure_has_only_fixed_safe_rule(mutation, rule):
    checked = verdict()
    if mutation == 'unknown': checked['checks'][0]['citation_ids'] = ['private-source']
    if mutation == 'required': checked['requirement_checks'][0]['point_id'] = 'private-point'
    if mutation == 'missing': del checked['checks'][0]['issue_type']
    with pytest.raises(VerificationError) as error:
        run(Model([draft()]),Model([checked]))
    assert error.value.details['schema_rule'] == rule
    assert 'private' not in json.dumps(error.value.details)
    assert VerificationError('FAILED', details={'schema_rule':'private-string'}).details == {}


class JournalModel(Model):
    def __init__(self, outputs, journal, stage):
        super().__init__(outputs)
        self.journal, self.stage = journal, stage

    def generate_json(self, *args, **kwargs):
        call_id = self.journal.begin_call(self.stage)
        value = super().generate_json(*args, **kwargs)
        self.journal.finish_call(call_id, status='succeeded', usage={'completion_tokens':20})
        self.journal.annotate_last(self.stage, metadata={'validation':'content_passed'})
        return value


@pytest.mark.parametrize('stage', ['generation', 'verification'])
def test_local_contract_rejection_marks_completed_physical_call_validation_failed(stage):
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    journal = RequestJournal()
    generated, checked = draft(), verdict()
    if stage == 'generation': generated['claims'][0]['citation_ids'] = ['private-source']
    else: checked['checks'][0]['evidence_spans'][0]['evidence_id'] = 'private-invented-id'
    with pytest.raises(VerificationError):
        run(JournalModel([generated], journal, 'generation'), JournalModel([checked], journal, 'verification'))
    snapshot = journal.snapshot()
    assert snapshot['failure_kind'] == 'response_schema'
    row = snapshot['calls'][-1]
    assert row['stage'] == stage
    assert row['validation'] == 'failed'
    assert row['failure_kind'] == 'response_schema'
    assert row['schema_rule'] == ('citation_identity' if stage == 'generation' else 'span_not_found')
    assert row['usage'] == {'completion_tokens':20}
    assert 'private' not in json.dumps(snapshot)


def test_successful_local_contracts_upgrade_content_only_journal_validation():
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    journal = RequestJournal()
    run(JournalModel([draft()], journal, 'generation'), JournalModel([verdict()], journal, 'verification'))
    assert [row['validation'] for row in journal.snapshot()['calls']] == ['passed','passed']


@pytest.mark.parametrize('text', ['未成年人的原\n则。', '  A\r\n\r\nB。\tC  ',
    'age18. unrelated99.', 'x  =  1\n  y = x + 1\n', '😀' * 1000])
def test_all_segment_text_preserves_full_original_without_normalization(text):
    verifier = AnswerVerifier(Model([]),Model([]))
    first, check = verifier.initial_messages('说明？', [dict(SOURCE,source_text=text)])
    assert json.loads(first[1]['content'])['evidence'] == [dict(chunk_id='s1',source_text=text)]
    evidence = json.loads(check[1]['content'])['evidence'][0]
    assert set(evidence) == {'chunk_id','segments'}
    assert ''.join(part['text'] for part in evidence['segments']) == text
    assert len({part['evidence_id'] for part in evidence['segments']}) == len(evidence['segments'])


def test_pdf_line_wrap_uses_adjacent_anchors_without_retyping_source():
    text = '应当遵循最有利于未成年人的原\n则。'
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = [dict(evidence_id='e1'),dict(evidence_id='e2')]
    result = AnswerVerifier(Model([draft(text='应当遵循最有利于未成年人的原则。')]),Model([checked])).answer(
        '遵循何原则？', [dict(SOURCE,source_text=text)], deadline=time.monotonic()+30)
    spans = result['trace']['verdicts'][0]['checks'][0]['evidence_spans']
    assert ''.join(text[span['start']:span['end']] for span in spans) == text
    assert result['status'] == 'answered'


def test_segment_cannot_belong_to_an_uncited_source():
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = [dict(evidence_id='e2')]
    with pytest.raises(VerificationError) as error:
        AnswerVerifier(Model([draft()]),Model([checked])).answer('说明？',
            [SOURCE,dict(SOURCE,chunk_id='other')], deadline=time.monotonic()+30)
    assert error.value.details['schema_rule'] == 'citation_relationship'


@pytest.mark.parametrize('code,kind', [('RAG_DEADLINE_EXCEEDED','deadline'),
    ('RAG_STAGE_BUDGET_EXCEEDED','stage_budget'), ('MODEL_OUTPUT_CAPACITY_EXCEEDED','output_budget'),
    ('MODEL_UNAVAILABLE','internal_error')])
def test_journal_error_without_details_is_not_misclassified_as_schema(code, kind):
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    journal = RequestJournal()
    original = VerificationError(code)
    with pytest.raises(VerificationError):
        run(JournalModel([original],journal,'generation'), JournalModel([],journal,'verification'))
    assert journal.snapshot()['failure_kind'] == kind
    assert journal.snapshot()['calls'][0]['failure_kind'] == kind
    assert 'schema_rule' not in journal.snapshot()['calls'][0]
    assert original.details == {}
