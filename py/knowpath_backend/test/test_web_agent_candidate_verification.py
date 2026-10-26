"""Agent drafts are checked once without invoking the legacy generator."""
import time

import pytest

from knowpath_backend.learning.rag.verification import AnswerVerifier, Draft, WireDraft, VerificationError
from knowpath_backend.test.test_rag_verification import Model, SOURCES, answer, verdict
from knowpath_backend.test.test_rag_verification_v2 import Model as V2Model, SOURCE, draft, verdict as v2verdict


class ForbiddenGenerator:
    def generate_json(self, *args, **kwargs):
        pytest.fail('checker-only candidate verification invoked generator')


def verify(candidate, checked, sources=SOURCES, checker_type=Model):
    checker = checker_type([checked])
    verifier = AnswerVerifier(ForbiddenGenerator(), checker)
    method = getattr(verifier, 'verify_candidate', None)
    assert callable(method), 'checker-only API missing'
    result = method('条件？', sources, candidate, deadline=time.monotonic() + 10)
    assert result['trace']['generation_calls'] == result['trace']['revisions'] == 0
    assert result['trace']['verification_calls'] == len(checker.calls) == 1
    return result


@pytest.mark.parametrize('supported,status', [('supported', 'answered'), ('unsupported', 'insufficient')])
def test_candidate_one_check_no_generation_or_revision(supported, status):
    assert verify(Draft.model_validate(answer()), verdict(supported))['status'] == status


def test_candidate_rejects_forged_citation_before_checker():
    candidate = answer()
    candidate['claims'][0]['citation_ids'] = ['forged']
    checker = Model([])
    verifier = AnswerVerifier(ForbiddenGenerator(), checker)
    assert callable(getattr(verifier, 'verify_candidate', None))
    with pytest.raises(VerificationError, match='GENERATION_INVALID_RESPONSE'):
        verifier.verify_candidate('条件？', SOURCES, candidate, deadline=time.monotonic()+10)
    assert checker.calls == []


def test_candidate_dependency_pruning_and_numeric_provenance():
    candidate = answer('期限99天。')
    candidate['claims'].append(dict(claim_id='derived', text='推导结论', kind='inference',
        citation_ids=['c1'], depends_on=['claim1']))
    checked = verdict()
    checked['checks'].append(dict(claim_id='derived', status='supported', citation_ids=['c1'], reason='推导'))
    result = verify(candidate, checked, [dict(chunk_id='c1', source_text='期限30天。')])
    assert result['status'] == 'insufficient'
    assert result['claims'] == []
    assert result['trace']['verdicts'][0]['checks'][0]['reason'] == 'NUMERIC_FACT_NOT_IN_CITED_SOURCE'


def test_candidate_wire_required_points_qualifier_and_short_id_mapping():
    result = verify(WireDraft.model_validate(draft()), v2verdict(), [SOURCE], V2Model)
    assert result['citation_ids'] == [SOURCE['chunk_id']]
    assert result['status'] == 'answered'
    assert result['trace']['verdicts'][0]['requirement_checks'][0]['status'] == 'supported'


@pytest.mark.parametrize('reason,status', [('evidence_missing', 'insufficient'), ('clarification_needed', 'clarify')])
def test_candidate_wire_nonanswer_checked_once(reason, status):
    result = verify(WireDraft.model_validate(draft(status)), v2verdict(reason), [SOURCE], V2Model)
    assert result['status'] == status


def test_candidate_missing_required_point_fails_without_retry():
    checked = v2verdict()
    checked['requirement_checks'][0]['point_id'] = 'p2'
    checker = V2Model([checked])
    verifier = AnswerVerifier(ForbiddenGenerator(), checker)
    assert callable(getattr(verifier, 'verify_candidate', None))
    with pytest.raises(VerificationError, match='VERIFICATION_UNAVAILABLE'):
        verifier.verify_candidate('条件？', [SOURCE], WireDraft.model_validate(draft()), deadline=time.monotonic()+10)
    assert len(checker.calls) == 1


def test_candidate_qualifier_failure_cannot_publish_supported_claim():
    checked = v2verdict()
    checked['checks'][0]['qualifier_checks']['conditions'] = 'violated'
    checked['checks'][0]['issue_type'] = 'qualifier_missing'
    assert verify(WireDraft.model_validate(draft()), checked, [SOURCE], V2Model)['claims'] == []


def test_candidate_partial_keeps_only_independently_supported_claim():
    candidate = answer()
    candidate['claims'].append(dict(claim_id='other', text='额外结论', kind='fact',
        citation_ids=['c1'], depends_on=[]))
    checked = verdict()
    checked['checks'].append(dict(claim_id='other', status='unsupported', citation_ids=['c1'], reason='不支持'))
    checked['complete'] = False
    result = verify(candidate, checked)
    assert result['status'] == 'partial'
    assert [claim['claim_id'] for claim in result['claims']] == ['claim1']


@pytest.mark.parametrize('foreign', ['s99', SOURCE['chunk_id']])
def test_wire_candidate_rejects_foreign_or_full_id_before_checker(foreign):
    candidate = draft()
    candidate['claims'][0]['citation_ids'] = [foreign]
    checker = V2Model([])
    verifier = AnswerVerifier(ForbiddenGenerator(), checker)
    with pytest.raises(VerificationError, match='GENERATION_INVALID_RESPONSE'):
        verifier.verify_candidate('条件？', [SOURCE], WireDraft.model_validate(candidate), deadline=time.monotonic()+10)
    assert checker.calls == []


def test_canonical_draft_remains_full_id_when_generator_has_v2_protocol():
    canonical = draft()
    canonical['claims'][0]['citation_ids'] = [SOURCE['chunk_id']]
    generator = ForbiddenGenerator()
    generator.protocol_version = 2
    checker = V2Model([v2verdict()])
    result = AnswerVerifier(generator, checker).verify_candidate('条件？', [SOURCE],
        Draft.model_validate(canonical), deadline=time.monotonic()+10)
    assert result['status'] == 'answered'
    assert result['citation_ids'] == [SOURCE['chunk_id']]


def test_candidate_checker_failure_does_not_retry_or_restore_other_state():
    checker = V2Model([RuntimeError('offline checker failed')])
    verifier = AnswerVerifier(ForbiddenGenerator(), checker)
    with pytest.raises(RuntimeError, match='offline checker failed'):
        verifier.verify_candidate('条件？', [SOURCE], WireDraft.model_validate(draft()), deadline=time.monotonic()+10)
    assert len(checker.calls) == 1
