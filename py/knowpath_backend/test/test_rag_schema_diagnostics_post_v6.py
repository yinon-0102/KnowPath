"""Apply post-v6-safe-schema-diagnostics.patch before running these tests."""
import copy
import json

import pytest

from knowpath_backend.learning.rag.diagnostics import RequestJournal, safe_metadata
from knowpath_backend.learning.rag.verification import VerificationError, _schema_details
from knowpath_backend.test.test_rag_verification import Model as LegacyModel, answer as legacy_answer
from knowpath_backend.test.test_rag_verification_v2 import Model, JournalModel, draft, verdict, run


@pytest.mark.parametrize('mutation,error_type,path', [
    ('missing', 'missing', 'claims[].kind'),
    ('literal', 'literal_error', 'claims[].kind'),
    ('long', 'string_too_long', 'claims[].text'),
    ('empty', 'too_short', 'claims[].citation_ids'),
    ('extra', 'extra_forbidden', 'claims[]'),
    ('root_extra', 'extra_forbidden', '$'),
    ('path_spoof', 'extra_forbidden', '$'),
])
def test_pydantic_diagnostics_use_fixed_types_and_sanitized_locations(mutation, error_type, path):
    raw = draft()
    if mutation == 'missing': del raw['claims'][0]['kind']
    if mutation == 'literal': raw['claims'][0]['kind'] = 'private-secret-value'
    if mutation == 'long': raw['claims'][0]['text'] = 'private-secret-text' * 100
    if mutation == 'empty': raw['claims'][0]['citation_ids'] = []
    if mutation == 'extra': raw['claims'][0]['private-extra-field'] = {'private':'secret'}
    if mutation == 'root_extra': raw['private-root-field'] = 'secret'
    if mutation == 'path_spoof': raw['claims[].text'] = 'private-secret'
    with pytest.raises(VerificationError) as caught:
        run(Model([raw]),Model([]))
    details = caught.value.details
    assert details['schema_error_type'] == error_type
    assert details['schema_error_path'] == path
    assert details['schema_rule'] == 'schema_fields'
    assert caught.value.code == 'GENERATION_INVALID_RESPONSE'
    assert 'private' not in json.dumps(details)


@pytest.mark.parametrize('mutation,error_type,path', [
    ('duplicate_claim', 'duplicate_claim_identity', 'claims'),
    ('duplicate_point', 'duplicate_point_identity', 'required_points'),
    ('dependency', 'dependency_order', 'claims[].depends_on'),
    ('nonanswer', 'nonanswer_contains_claims', 'claims'),
    ('empty_answer', 'answer_without_claims', 'claims'),
])
def test_invariant_failure_categories_preserve_rejection(mutation, error_type, path):
    raw = draft()
    if mutation == 'duplicate_claim': raw['claims'].append(copy.deepcopy(raw['claims'][0]))
    if mutation == 'duplicate_point': raw['required_points'].append(copy.deepcopy(raw['required_points'][0]))
    if mutation == 'dependency': raw['claims'][0]['depends_on'] = ['private-unavailable-claim']
    if mutation == 'nonanswer': raw['status'] = 'insufficient'
    if mutation == 'empty_answer': raw['claims'] = []
    with pytest.raises(VerificationError) as caught:
        run(Model([raw]),Model([]))
    assert caught.value.code == 'GENERATION_INVALID_RESPONSE'
    assert caught.value.details['schema_error_type'] == error_type
    assert caught.value.details['schema_error_path'] == path


def test_nested_check_error_reaches_call_journal_without_response_data():
    raw = verdict()
    raw['checks'][0]['qualifier_checks']['conditions'] = 'private-unsupported-value'
    journal = RequestJournal()
    with pytest.raises(VerificationError) as caught:
        run(JournalModel([draft()],journal,'generation'), JournalModel([raw],journal,'verification'))
    row = journal.snapshot()['calls'][-1]
    for key, expected in {'schema_error_type':'literal_error',
                          'schema_error_path':'checks[].qualifier_checks.conditions'}.items():
        assert caught.value.details[key] == row[key] == expected
    assert row['validation'] == 'failed'
    assert row['usage'] == {'completion_tokens':20}
    assert 'private' not in json.dumps(journal.snapshot())


def test_allowed_fields_are_checked_again_at_public_and_journal_boundaries():
    unsafe = {'schema_error_type':'private-type', 'schema_error_path':'claims[].private-key',
              'input':'private-input', 'ctx':{'error':'private-error'}, 'msg':'private-message'}
    assert VerificationError('FAILED',details=unsafe).details == {}
    assert safe_metadata(unsafe) == {}
    safe = {'schema_error_type':'missing', 'schema_error_path':'claims[].text'}
    assert VerificationError('FAILED',details=safe).details == safe_metadata(safe) == safe


def test_unknown_validation_error_type_and_context_are_not_exposed():
    from pydantic import BaseModel, ValidationError, model_validator
    from pydantic_core import PydanticCustomError
    class UntrustedError(BaseModel):
        @model_validator(mode='after')
        def invalid(self):
            raise PydanticCustomError('private-type', 'private-message', {'private-key':'private-value'})
    with pytest.raises(ValidationError) as caught:
        UntrustedError.model_validate({})
    assert _schema_details(caught.value,Model([])) == {
        'schema_rule':'schema_fields', 'schema_error_type':'other', 'schema_error_path':'$'}


def test_legacy_error_shape_and_successful_protocol_requests_remain_unchanged():
    invalid = legacy_answer()
    invalid['claims'][0]['kind'] = 'invalid'
    with pytest.raises(VerificationError) as caught:
        run(LegacyModel([invalid]),LegacyModel([]))
    assert caught.value.details == {'stage':'generation','call_index':1,'failure_kind':'response_schema'}
    generator, checker = Model([draft()]),Model([verdict()])
    result = run(generator,checker)
    assert result['status'] == 'answered'
    assert len(generator.calls) == len(checker.calls) == 1
    assert 'schema_error' not in json.dumps(generator.calls + checker.calls)


@pytest.mark.parametrize('case,subcategory', [
    ('check_set', 'check_set_mismatch'),
    ('foreign_check', 'check_sources_out_of_scope'),
    ('empty_supported', 'supported_without_citation'),
    ('different_supported', 'supported_citations_differ_from_draft'),
    ('requirement_set', 'requirement_set_mismatch'),
    ('requirement_reference', 'requirement_reference_invalid'),
])
def test_contract_invariants_have_fixed_fine_diagnostics(case, subcategory):
    import time
    from knowpath_backend.learning.rag.verification import AnswerVerifier
    from knowpath_backend.test.test_rag_verification_v2 import SOURCE
    raw = verdict()
    # Exercise local reference checks without wire citation hydration and
    # verify the new scalar survives both sanitization boundaries.
    raw['checks'][0]['citation_ids'] = [SOURCE['chunk_id']]
    raw['checks'][0]['evidence_spans'] = []
    raw['requirement_checks'][0].update(text='必要申请条件及例外', reason='checked',
        citation_ids=[SOURCE['chunk_id']])
    raw['checks'][0]['reason'] = 'checked'
    if case == 'check_set': raw['checks'][0]['claim_id'] = 'c2'
    if case == 'foreign_check': raw['checks'][0]['citation_ids'] = ['private-foreign']
    if case == 'empty_supported': raw['checks'][0]['citation_ids'] = []
    if case == 'different_supported': raw['checks'][0]['citation_ids'] = ['other']
    if case == 'requirement_set': raw['requirement_checks'][0]['point_id'] = 'p9'
    if case == 'requirement_reference': raw['requirement_checks'][0]['claim_ids'] = ['private-claim']
    checker = LegacyModel([raw])
    generated = draft()
    generated['claims'][0]['citation_ids'] = [SOURCE['chunk_id']]
    with pytest.raises(VerificationError) as caught:
        AnswerVerifier(LegacyModel([generated]), checker).answer('问题',
            [SOURCE, dict(SOURCE, chunk_id='other')], deadline=time.monotonic()+30)
    assert caught.value.details['schema_subcategory'] == subcategory
    assert safe_metadata(caught.value.details)['schema_subcategory'] == subcategory
    assert 'private' not in json.dumps(caught.value.details)


def test_fine_diagnostic_allowlist_rejects_arbitrary_values():
    unsafe = {'schema_subcategory': 'private-diagnostic'}
    assert VerificationError('FAILED', details=unsafe).details == safe_metadata(unsafe) == {}
