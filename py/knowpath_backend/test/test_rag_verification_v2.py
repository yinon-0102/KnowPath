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


class RealLikeModel(Model):
    allows_contract_retry = True


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
    with pytest.raises(RuntimeError, match='transport failed'):
        run(generator, checker)
    assert len(generator.calls) == 1
    assert checker.calls == []


def partial_round():
    first = draft()
    first['required_points'].append(dict(point_id='p2', text='尚未完成的补充要点'))
    checked = verdict('answer_incomplete')
    checked['missing_points'] = ['p2']
    checked['requirement_checks'].append(dict(point_id='p2', status='undetermined',
        citation_ids=[], claim_ids=[]))
    return first, checked


@pytest.mark.parametrize('stage', ['generation', 'verification'])
def test_terminal_contract_error_preserves_only_previous_verified_partial(stage):
    first, good = partial_round()
    second = copy.deepcopy(first)
    second['claims'][0]['text'] = '本轮失败内容不得替换旧结论。'
    bad = copy.deepcopy(good)
    if stage == 'generation':
        second['missing_points'] = [{'point_id': 'p2'}]
    else:
        bad['missing_points'] = [{'point_id': 'p2'}]
    generator = RealLikeModel([first, second])
    checker = RealLikeModel([good, bad])
    result = run(generator, checker)
    assert result['status'] == 'partial'
    assert result['claims'] == list(result['trace']['drafts'][0]['claims'])
    assert result['citation_ids'] == [SOURCE['chunk_id']]
    assert result['text'].startswith('以下为已核验的部分内容，其余部分暂未完成可靠核验')
    assert '本轮失败内容' not in result['text']
    assert result['trace']['delivery'] == dict(reason=f'{stage}_contract_failed', recovered=True,
        recovery_decision='restored', snapshot_draft_index=0, snapshot_verdict_index=0, retained_claim_count=1)
    assert len(generator.calls) == 2
    assert len(checker.calls) == (1 if stage == 'generation' else 2)


def test_new_valid_requirement_prevents_restoring_stale_partial():
    first, good = partial_round()
    second = copy.deepcopy(first)
    second['required_points'].append(dict(point_id='p3', text='合法新增要求'))
    bad = copy.deepcopy(good)
    bad['missing_points'] = [{'point_id': 'p2'}]
    result = run(RealLikeModel([first, second]), RealLikeModel([good, bad]))
    assert result['status'] == 'insufficient' and result['claims'] == []
    assert result['text'] == '本次回答未完成可靠核验，请重试'
    assert result['trace']['delivery']['recovery_decision'] == 'requirements_changed'


@pytest.mark.parametrize('defect', ['numeric', 'qualifier', 'dependency'])
def test_contract_fallback_never_resurrects_locally_pruned_claims(defect):
    first, good = partial_round()
    if defect == 'numeric':
        first['claims'][0]['text'] = '年满99岁可申请。'
    else:
        good['checks'][0]['qualifier_checks']['conditions'] = 'violated'
        if defect == 'dependency':
            first['claims'].append(dict(first['claims'][0], claim_id='c2', depends_on=['c1']))
            good['checks'].append(dict(verdict()['checks'][0], claim_id='c2'))
    bad = copy.deepcopy(good)
    bad['missing_points'] = [{'point_id': 'p2'}]
    result = run(RealLikeModel([first, first]), RealLikeModel([good, bad]))
    assert result['claims'] == []
    assert result['trace']['delivery']['recovery_decision'] == 'no_verified_snapshot'
    assert result['trace']['delivery']['recovered'] is False


@pytest.mark.parametrize('later', ['denied', 'undetermined', 'nonanswer', 'changed_text'])
def test_later_valid_semantic_result_always_wins(later):
    first, good = partial_round()
    second, checked = copy.deepcopy(first), copy.deepcopy(good)
    if later in {'denied', 'undetermined'}:
        checked['checks'][0]['status'] = 'unsupported' if later == 'denied' else 'undetermined'
    elif later == 'nonanswer':
        second.update(status='insufficient', claims=[])
        checked.update(checks=[], reason_code='evidence_missing')
        checked['requirement_checks'][0].update(status='undetermined', citation_ids=[], claim_ids=[])
    else:
        second['claims'][0]['text'] = '培训合格是申请条件。'
    result = run(RealLikeModel([first, second]), RealLikeModel([good, checked]))
    assert result['trace']['delivery']['reason'] == 'semantic_result'
    assert result['trace']['delivery']['recovered'] is False
    if later == 'changed_text':
        assert result['claims'][0]['text'] == second['claims'][0]['text']
    else:
        assert result['claims'] == []


@pytest.mark.parametrize('stage', ['generation', 'verification'])
@pytest.mark.parametrize('error', [RuntimeError('internal bug'),
    VerificationError('RAG_CANCELLED'), VerificationError('MODEL_TOKEN_BUDGET_EXCEEDED'),
    VerificationError('MODEL_UNAVAILABLE', details={'failure_kind':'authentication'}),
    VerificationError('MODEL_UNAVAILABLE', details={'failure_kind':'network_error'}),
    VerificationError('RAG_DEADLINE_EXCEEDED')])
def test_noncontract_terminal_errors_propagate_despite_trusted_snapshot(stage, error):
    first, good = partial_round()
    generator = RealLikeModel([first, error if stage == 'generation' else first])
    checker = RealLikeModel([good, error])
    with pytest.raises(type(error), match=str(error)):
        run(generator, checker)


def test_restoration_rechecks_deadline_after_malformed_checker_response(monkeypatch):
    from knowpath_backend.learning.rag import verification
    first, good = partial_round()
    bad = copy.deepcopy(good)
    bad['missing_points'] = [{}]
    class ExpiringModel(RealLikeModel):
        def generate_json(self, *args, **kwargs):
            output = super().generate_json(*args, **kwargs)
            if len(self.calls) == 2:
                monkeypatch.setattr(verification.time, 'monotonic', lambda: 100)
            return output
    monkeypatch.setattr(verification.time, 'monotonic', lambda: 0)
    with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
        AnswerVerifier(RealLikeModel([first, first]), ExpiringModel([good, bad])).answer(
            '问题', [SOURCE], deadline=30)


def test_snapshot_never_crosses_answer_calls():
    first, good = partial_round()
    bad = copy.deepcopy(good)
    bad['missing_points'] = [{}]
    verifier = AnswerVerifier(RealLikeModel([first] * 4), RealLikeModel([good, bad, bad, bad]))
    restored = verifier.answer('问题', [SOURCE], deadline=time.monotonic()+30)
    empty = verifier.answer('问题', [SOURCE], deadline=time.monotonic()+30)
    assert restored['trace']['delivery']['recovered'] is True
    assert empty['claims'] == []
    assert empty['trace']['delivery']['recovery_decision'] == 'no_verified_snapshot'


def test_invalid_draft_citation_does_not_commit_its_requirement_list():
    from knowpath_backend.test.test_rag_verification import Model as LegacyModel
    class RetryLegacyModel(LegacyModel):
        allows_contract_retry = True
    bad = draft()
    bad['claims'][0]['citation_ids'] = ['invalid-source']
    bad['required_points'].append(dict(point_id='poison', text='不得进入清单'))
    valid = draft()
    valid['claims'][0]['citation_ids'] = [SOURCE['chunk_id']]
    result = run(RetryLegacyModel([bad, valid]), Model([verdict()]))
    assert result['status'] == 'answered'
    assert [p['point_id'] for p in result['trace']['drafts'][0]['required_points']] == ['p1']


def test_fallback_preserves_failed_journal_and_previous_checked_verdict():
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    first, good = partial_round()
    bad = copy.deepcopy(good)
    bad['missing_points'] = [{}]
    journal = RequestJournal()
    generator, checker = JournalModel([first, first], journal, 'generation'), JournalModel([good, bad], journal, 'verification')
    generator.allows_contract_retry = checker.allows_contract_retry = True
    result = run(generator, checker)
    assert result['trace']['delivery']['recovered'] is True
    assert len(result['trace']['verdicts']) == 1
    assert result['trace']['verdicts'][0]['requirement_checks'][1]['status'] == 'undetermined'
    assert journal.snapshot()['calls'][-1]['validation'] == 'failed'
    assert journal.snapshot()['failure_kind'] == 'response_schema'


def test_recovery_snapshot_contains_only_post_pruning_claims_and_demoted_requirements():
    first, good = partial_round()
    first['claims'].append(dict(first['claims'][0], claim_id='c2', text='年龄99岁。'))
    first['claims'].append(dict(first['claims'][0], claim_id='c3', depends_on=['c2']))
    good['checks'] += [dict(copy.deepcopy(good['checks'][0]), claim_id=identifier) for identifier in ('c2', 'c3')]
    good['requirement_checks'][1].update(status='supported', citation_ids=['s1'], claim_ids=['c3'])
    bad = copy.deepcopy(good)
    bad['missing_points'] = [{}]
    result = run(RealLikeModel([first, first]), RealLikeModel([good, bad]))
    assert [claim['claim_id'] for claim in result['claims']] == ['c1']
    assert result['trace']['verdicts'][0]['requirement_checks'][1]['status'] == 'unsupported'
    assert result['trace']['delivery']['retained_claim_count'] == 1


def test_source_identity_mutation_prevents_snapshot_recovery():
    first, good = partial_round()
    bad = copy.deepcopy(good)
    bad['missing_points'] = [{}]
    source = copy.deepcopy(SOURCE)
    class MutatingChecker(RealLikeModel):
        def generate_json(self, *args, **kwargs):
            output = super().generate_json(*args, **kwargs)
            if len(self.calls) == 2:
                source['source_text'] = '已改变的原文'
            return output
    result = AnswerVerifier(RealLikeModel([first, first]), MutatingChecker([good, bad])).answer(
        '问题', [source], deadline=time.monotonic()+30)
    assert result['claims'] == []
    assert result['trace']['delivery']['recovery_decision'] == 'input_identity_changed'


def test_malformed_new_requirement_does_not_block_old_snapshot_recovery():
    first, good = partial_round()
    invalid = copy.deepcopy(first)
    invalid['required_points'].append(dict(point_id='p3', text={'bad': 'shape'}))
    result = run(RealLikeModel([first, invalid]), RealLikeModel([good]))
    assert result['trace']['delivery']['recovery_decision'] == 'restored'
    assert len(result['claims']) == 1


def test_terminal_normalized_nonanswer_cannot_drop_canonical_requirements():
    first, good = partial_round()
    invalid = draft()
    invalid['status'] = 'insufficient'
    # This terminal nonanswer is eligible for existing drop-claims repair,
    # but repairing claims must not waive checklist identity validation.
    result = run(RealLikeModel([first, invalid]), RealLikeModel([good]))
    assert result['trace']['delivery']['reason'] == 'generation_contract_failed'
    assert result['trace']['delivery']['recovered'] is True
    assert result['claims'][0]['text'] == first['claims'][0]['text']


def test_contradictory_completion_reason_is_explicit_contract_failure():
    first, good = partial_round()
    bad = copy.deepcopy(good)
    bad['complete'] = True
    result = run(RealLikeModel([first, first]), RealLikeModel([good, bad]))
    assert result['trace']['delivery']['recovered'] is True


@pytest.mark.parametrize('stage', ['generation', 'verification'])
def test_internal_exceptions_keep_internal_journal_classification(stage):
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    journal = RequestJournal()
    generator = JournalModel([RuntimeError('private-error') if stage == 'generation' else draft()], journal, 'generation')
    checker = JournalModel([RuntimeError('private-error')], journal, 'verification')
    generator.allows_contract_retry = checker.allows_contract_retry = True
    with pytest.raises(RuntimeError):
        run(generator, checker)
    row = journal.snapshot()['calls'][-1]
    assert row['failure_kind'] == 'internal_error'
    assert 'schema_rule' not in row
    assert 'private' not in json.dumps(journal.snapshot())


def test_fine_contract_subcategory_survives_failed_physical_call_journal():
    from knowpath_backend.learning.rag.diagnostics import RequestJournal, safe_journal_snapshot
    journal = RequestJournal()
    bad = verdict()
    bad['checks'][0]['claim_id'] = 'c2'
    with pytest.raises(VerificationError) as error:
        run(JournalModel([draft()], journal, 'generation'), JournalModel([bad], journal, 'verification'))
    assert error.value.details['schema_subcategory'] == 'check_set_mismatch'
    assert safe_journal_snapshot(journal.snapshot())['calls'][-1]['schema_subcategory'] == 'check_set_mismatch'


def test_real_provider_terminal_generation_schema_failure_degrades_to_safe_nonanswer():
    bad = draft()
    bad['claims'][0]['citation_ids'] = []
    generator = Model([bad, bad])
    generator.allows_contract_retry = True
    result = run(generator, Model([]))
    assert result['status'] == 'insufficient'
    assert result['claims'] == []
    assert result['trace']['contract_repairs'] == [
        {'stage': 'generation', 'kind': 'safe_nonanswer_fallback'}]


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


def test_checker_wire_payload_normalizes_string_evidence_ids_and_ignores_diagnostic_extras():
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = ['e1']
    checked['requirement_checks'][0]['evidence_spans'] = ['e1']
    result = AnswerVerifier(Model([draft()]), Model([checked])).answer(
        '申请有哪些条件？', [SOURCE], deadline=time.monotonic() + 30,
        max_generation_calls=1, max_verification_calls=1)
    assert result['status'] == 'answered'
    assert result['trace']['verdicts'][0]['checks'][0]['evidence_spans'][0]['start'] == 0


def test_checker_wire_payload_accepts_common_status_aliases_without_relaxing_contract():
    checked = verdict()
    checked['checks'][0]['status'] = 'pass'
    checked['requirement_checks'][0]['status'] = 'pass'
    result = run(Model([draft()]), Model([checked]))
    assert result['status'] == 'answered'


def test_checker_replaces_rephrased_existing_requirement_text_with_local_canonical_text():
    checked = verdict()
    checked['requirement_checks'][0]['text'] = '模型改写的必要申请条件及例外'
    result = run(Model([draft()]), Model([checked]))
    assert result['status'] == 'answered'
    assert result['trace']['verdicts'][0]['requirement_checks'][0]['text'] == '必要申请条件及例外'


def test_checker_maps_partial_status_to_undetermined():
    checked = verdict()
    checked['requirement_checks'][0]['status'] = 'partially_supported'
    result = AnswerVerifier(Model([draft()]), Model([checked])).answer(
        '申请有哪些条件？', [SOURCE], deadline=time.monotonic() + 30,
        max_generation_calls=1, max_verification_calls=1)
    assert result['status'] == 'partial'


def test_checker_maps_unknown_text_status_to_undetermined():
    checked = verdict()
    checked['requirement_checks'][0]['status'] = 'provider_specific_label'
    result = AnswerVerifier(Model([draft()]), Model([checked])).answer(
        '申请有哪些条件？', [SOURCE], deadline=time.monotonic() + 30,
        max_generation_calls=1, max_verification_calls=1)
    assert result['status'] == 'partial'


def test_real_provider_contract_failure_degrades_to_safe_nonanswer():
    checked = verdict()
    checked['checks'][0]['claim_id'] = 'c2'
    result = AnswerVerifier(Model([draft()]), RealLikeModel([checked])).answer(
        '申请有哪些条件？', [SOURCE], deadline=time.monotonic() + 30,
        max_generation_calls=1, max_verification_calls=1)
    assert result['status'] == 'insufficient'
    assert result['claims'] == []


def test_real_provider_terminal_verification_span_failure_degrades_to_safe_nonanswer():
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = []
    result = AnswerVerifier(Model([draft(), draft()]), RealLikeModel([checked, checked])).answer(
        '申请有哪些条件？', [SOURCE], deadline=time.monotonic() + 30)
    assert result['status'] == 'insufficient'
    assert result['claims'] == []


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
    with pytest.raises(RuntimeError, match='private service payload'):
        run(generator, checker)


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
    assert set(checked['checks'][0]) >= set(verdict()['checks'][0]) - {'citation_ids'}
    assert 'citation_ids' not in checked['checks'][0]
    assert 'citation_ids' not in checked['requirement_checks'][0]


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
