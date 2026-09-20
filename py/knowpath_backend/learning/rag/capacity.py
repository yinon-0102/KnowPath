"""Admit complete generation/check pairs; never shrink an individual source."""
from copy import deepcopy
import json
import time

from .token_budget import ModelBudget, StageRequest, TokenBudgetExceeded, pack_evidence_groups
from .verification import VerificationError, WireDraft, WireVerdict


class AnswerCapacity:
    def __init__(self, verifier, *, generation_seconds, verification_seconds, spending=None):
        self.verifier = verifier
        self.generator, self.checker = verifier.generator, verifier.checker
        self.generation_seconds, self.verification_seconds = generation_seconds, verification_seconds
        self.last_trace = {}
        self.spending = spending

    @staticmethod
    def _budget(model):
        return ModelBudget(model.max_input_tokens, model.settings.context_budget_tokens, model.max_output_tokens)

    def _requests(self, generation, verification):
        return [StageRequest('generation', self.generator.request_body(generation, WireDraft.model_json_schema())),
                StageRequest('verification', self.checker.request_body(verification, WireVerdict.model_json_schema()),
                             reserved_input_tokens=2 * self.generator.max_draft_bytes)]

    def select_context(self, question, rows, *, max_evidence_tokens=5000):
        def requests(selected):
            source_bound = sum(len(row['source_text'].encode('utf-8')) for row in selected) + max(0, len(selected)-1)
            if source_bound > max_evidence_tokens:
                raise TokenBudgetExceeded('evidence_context_limit', stage='generation')
            return self._requests(*self.verifier.initial_messages(question, selected))
        try:
            packed = pack_evidence_groups(rows, build_requests=requests, profile=self.generator.counting_profile,
                budgets={'generation':self._budget(self.generator), 'verification':self._budget(self.checker)})
        except TokenBudgetExceeded as error:
            raise VerificationError('MODEL_TOKEN_BUDGET_EXCEEDED', details={
                'stage':error.stage or 'generation', 'failure_kind':'input_budget'}) from None
        self.last_trace = {'count_profile':self.generator.counting_profile.provenance,
            'draft_reserve_bytes':self.generator.max_draft_bytes,
            'omitted_chunk_ids':list(packed.omitted_chunk_ids), 'invalid_chunk_ids':list(packed.invalid_chunk_ids),
            'planned_inputs':{key:value.value for key,value in packed.stage_counts.items()}}
        return packed.rows, self.last_trace

    def admit_revision(self, generation, verification, *, deadline):
        if deadline-time.monotonic() < self.generation_seconds+self.verification_seconds:
            raise VerificationError('RAG_STAGE_BUDGET_EXCEEDED', details={'stage':'generation'})
        # The old draft does not bound the revised draft. Remove it and reserve
        # the separately enforced canonical JSON byte cap, doubled for the
        # outer message JSON escaping. No token-to-byte conversion is assumed.
        template = deepcopy(verification)
        payload = json.loads(template[-1]['content'])
        if isinstance(payload, dict): payload['draft'] = {}
        template[-1]['content'] = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        try:
            requests = self._requests(generation, template)
            for request in requests:
                model = self.generator if request.stage == 'generation' else self.checker
                self._budget(model).check(model.counting_profile.count_request(request.body),
                    reserved_input_tokens=request.reserved_input_tokens, stage=request.stage)
        except TokenBudgetExceeded as error:
            raise VerificationError('MODEL_TOKEN_BUDGET_EXCEEDED', details={
                'stage':error.stage, 'failure_kind':'input_budget'}) from None
        if self.spending is not None:
            self.spending.reserve_model_pair([{'model':request.body['model'],
                'input_bound':len(json.dumps(request.body, ensure_ascii=False, separators=(',', ':')).encode())
                    + 1024 + request.reserved_input_tokens,
                'output_bound':request.body['max_tokens']} for request in requests])

    def admit_initial(self, question, sources, *, deadline):
        self.admit_revision(*self.verifier.initial_messages(question, sources), deadline=deadline)
