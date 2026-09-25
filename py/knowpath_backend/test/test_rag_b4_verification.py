"""Shared B4 prerequisites: numeric provenance and locally bound citations."""
from decimal import Decimal
import json
import time

import pytest

from knowpath_backend.learning.rag.verification import AnswerVerifier, VerificationError, numeric_values
from knowpath_backend.test.test_rag_verification_v2 import Model, RealLikeModel, draft, verdict


def run(text='原文结论。', source='原文结论。', *, generated=None, checked=None, sources=None, **kwargs):
    return AnswerVerifier(Model([generated or draft(text=text)]), Model([checked or verdict()]), **kwargs).answer(
        '说明。', sources or [{'chunk_id': 'source-one', 'source_text': source}],
        deadline=time.monotonic() + 30, max_generation_calls=1)


@pytest.mark.parametrize('text', ['构成价值取向的二元对举。', '这是二元运算。', '一般保持一致。'])
def test_conceptual_han_digits_do_not_override_semantic_support(text):
    assert run(text, '原文定义支持这一概念。')['status'] == 'answered'


@pytest.mark.parametrize('text,number', [('金额二元。', 2), ('罚款二元。', 2), ('年满二岁。', 2),
    ('二维空间。', 2), ('重复二次。', 2), ('比例20%。', 20), ('产品X11。', 11), ('公式x11。', 11)])
def test_real_quantities_and_unknown_alphanumerics_remain_checked(text, number):
    assert Decimal(number) in numeric_values(text)
    assert run(text)['status'] == 'insufficient'


def test_request_known_source_id_does_not_become_numeric_fact():
    sources = [{'chunk_id': f'source-{index}', 'source_text': '原文支持。'} for index in range(1, 12)]
    generated = draft(text='根据s11可知原文支持。')
    generated['claims'][0]['citation_ids'] = ['s11']
    checked = verdict()
    checked['checks'][0]['citation_ids'] = ['s11']
    checked['checks'][0]['evidence_spans'] = [{'evidence_id': 'e11'}]
    checked['requirement_checks'][0]['citation_ids'] = ['s11']
    assert run(generated=generated, checked=checked, sources=sources)['status'] == 'answered'


@pytest.mark.parametrize('text', ['来源s11表示数量11。', '型号xs11。', '未知来源s110。'])
def test_known_id_masking_never_erases_independent_or_embedded_numbers(text):
    assert Decimal(11 if '110' not in text else 110) in numeric_values(text, known_ids={'s11'})


def test_span_supported_subset_is_published_without_redundant_echo_sets():
    generated = draft(text='原文结论。')
    generated['claims'][0]['citation_ids'] = ['s1', 's2']
    checked = verdict()
    del checked['checks'][0]['citation_ids']
    del checked['requirement_checks'][0]['citation_ids']
    result = run(generated=generated, checked=checked, sources=[
        {'chunk_id': 'source-one', 'source_text': '原文结论。'},
        {'chunk_id': 'source-two', 'source_text': '无关内容。'}])
    assert result['status'] == 'answered'
    assert result['citation_ids'] == ['source-one']
    assert result['claims'][0]['citation_ids'] == ('source-one',)
    assert result['trace']['verdicts'][0]['requirement_checks'][0]['citation_ids'] == ('source-one',)


def test_duplicate_ids_are_deduplicated_before_wire_array_bounds():
    generated = draft(text='原文结论。')
    generated['claims'][0]['citation_ids'] = ['s1'] * 13
    checked = verdict()
    checked['checks'][0]['citation_ids'] *= 13
    checked['checks'][0]['evidence_spans'] *= 13
    checked['requirement_checks'][0]['citation_ids'] *= 13
    checked['requirement_checks'][0]['claim_ids'] *= 13
    result = run(generated=generated, checked=checked)
    assert result['status'] == 'answered'
    assert len(result['trace']['verdicts'][0]['checks'][0]['evidence_spans']) == 1


def test_unique_evidence_overflow_is_not_trimmed():
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = [{'evidence_id': f'e{i}'} for i in range(1, 14)]
    with pytest.raises(VerificationError):
        run(source='原文结论。' * 13, checked=checked)


def test_new_source_still_requires_revised_draft_and_reverification():
    sources = [{'chunk_id': 'one', 'source_text': '原文结论。'},
               {'chunk_id': 'two', 'source_text': '原文结论。'}]
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = [{'evidence_id': 'e2'}]
    first, revised = draft(text='原文结论。'), draft(text='原文结论。')
    revised['claims'][0]['citation_ids'] = ['s2']
    generator, checker = RealLikeModel([first, revised]), RealLikeModel([checked, checked])
    result = AnswerVerifier(generator, checker).answer('说明。', sources, deadline=time.monotonic()+30)
    assert result['status'] == 'answered'
    assert result['citation_ids'] == ['two']
    assert len(generator.calls) == len(checker.calls) == 2


def test_local_diagnostics_bind_raw_and_post_filter_objects_without_authentication():
    events = []
    checked = verdict()
    checked['checks'][0]['authorization'] = 'Bearer secret-token'
    checked['checks'][0]['reason'] = 'Authorization: Bearer secret-token'
    result = run('金额99元。', checked=checked, diagnostic_callback=events.append)
    assert result['status'] == 'insufficient'
    assert any(e['stage'] == 'verification' and e['phase'] == 'raw' for e in events)
    post = next(e for e in events if e['stage'] == 'verification' and e['phase'] == 'filtered')
    assert post['object']['checks'][0]['reason'] == 'NUMERIC_FACT_NOT_IN_CITED_SOURCE'
    assert post['categories'] == ['numeric_rejection']
    assert len(post['request_sha256']) == 64
    assert all(e['request_sha256'] == post['request_sha256'] for e in events)
    assert 'secret-token' not in json.dumps(events)


def test_contract_failure_diagnostic_keeps_exact_invalid_identity_and_path():
    events = []
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = [{'evidence_id': 'missing-anchor'}]
    with pytest.raises(VerificationError):
        run(checked=checked, diagnostic_callback=events.append)
    raw = next(e for e in events if e['stage'] == 'verification' and e['phase'] == 'raw')
    assert raw['object']['checks'][0]['evidence_spans'][0]['evidence_id'] == 'missing-anchor'
    failed = next(e for e in events if e['stage'] == 'verification' and e['phase'] == 'rejected')
    assert failed['categories'] == ['contract_invalid']
    assert failed['error']['schema_rule'] == 'span_not_found'


def test_prompt_uses_minimal_bound_ids_and_real_dependencies_only():
    from knowpath_backend.learning.rag.verification import GENERATION_V2_SYSTEM, VERIFICATION_V2_SYSTEM
    example = json.loads(VERIFICATION_V2_SYSTEM.split('\nJSON示例：')[1])
    assert 'citation_ids' not in example['checks'][0]
    assert 'citation_ids' not in example['requirement_checks'][0]
    assert '真实推理依赖' in GENERATION_V2_SYSTEM
    assert '无依赖时用空数组' in GENERATION_V2_SYSTEM


def test_unbound_or_uncited_identifiers_remain_rejected():
    checked = verdict()
    checked['checks'][0]['evidence_spans'] = [{'evidence_id': 'e2'}]
    with pytest.raises(VerificationError):
        run(checked=checked, sources=[{'chunk_id': 'one', 'source_text': '原文结论。'},
                                     {'chunk_id': 'two', 'source_text': '原文结论。'}])


def test_supported_dependency_is_pruned_when_premise_fails_and_diagnosed():
    generated, checked, events = draft(text='原文结论。'), verdict(), []
    generated['claims'].append(dict(claim_id='c2', text='推导结论。', kind='inference',
                                    citation_ids=['s1'], depends_on=['c1']))
    checked['checks'].append({**checked['checks'][0], 'claim_id': 'c2'})
    checked['checks'][0] = {**checked['checks'][0], 'status': 'unsupported', 'issue_type': 'unsupported_fact'}
    checked['requirement_checks'][0]['claim_ids'] = ['c2']
    result = run(generated=generated, checked=checked, diagnostic_callback=events.append)
    assert result['claims'] == []
    filtered = next(e for e in events if e['phase'] == 'filtered')
    assert filtered['categories'] == ['dependency_pruning', 'semantic_unsupported']
    assert filtered['object']['requirement_checks'][0]['status'] == 'unsupported'


@pytest.mark.parametrize('text', ['金额为二元人民币。', '费用为二元人民币。',
    '支付金额为 二 元人民币。', '费用是二元起。', '罚款为二元以上。', '价格为二元至五元。'])
def test_explicit_currency_with_copulas_and_suffixes_remains_numeric(text):
    assert Decimal(2) in numeric_values(text)
    assert run(text)['status'] == 'insufficient'


def test_exact_request_evidence_id_is_not_a_numeric_fact():
    assert run('根据e1可知原文结论。')['status'] == 'answered'


@pytest.mark.parametrize('text', ['根据xe1可知原文结论。', '根据e999可知原文结论。',
    '根据e1得出金额1元。'])
def test_evidence_id_masking_preserves_unknown_embedded_and_independent_numbers(text):
    assert run(text)['status'] == 'insufficient'


@pytest.mark.parametrize('secret', ['refresh_token=FAKE_REVIEW_TOKEN',
    'Cookie: session=FAKE_REVIEW_TOKEN; other=FAKE_REVIEW_TOKEN',
    '{"api_key":"FAKE_REVIEW_TOKEN"}', 'Authorization: FAKE_REVIEW_TOKEN',
    "{'refresh_token': 'FAKE_REVIEW_TOKEN'}", 'headers: {"Custom": "FAKE_REVIEW_TOKEN"}',
    'api_key = "FAKE_REVIEW_TOKEN with spaces"'])
def test_local_callback_redacts_embedded_json_headers_and_token_strings(secret):
    events, checked = [], verdict()
    checked['checks'][0]['reason'] = secret
    assert run(checked=checked, diagnostic_callback=events.append)['status'] == 'answered'
    assert any(e['phase'] == 'raw' for e in events)
    assert 'FAKE_REVIEW_TOKEN' not in json.dumps(events)


def test_local_sanitizer_preserves_noncredential_evidence_for_replay():
    from knowpath_backend.learning.rag.schema_diagnostics import sanitize_local_validation
    value = {'checks': [{'claim_id': 'c1', 'reason': 's11 / e1: amount 2; token budget 512',
                        'evidence_spans': [{'evidence_id': 'missing-evidence'}]}]}
    assert sanitize_local_validation(value) == value
