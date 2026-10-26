"""Business-to-Agent adapter; no legacy answer generation or candidate writes."""
from copy import deepcopy
from dataclasses import replace
import time

import httpx

from knowpath_backend.core.turn import TurnCancelled
from knowpath_backend.learning.agent_runtime.contracts import TurnRequest, TurnBudgetLimits
from knowpath_backend.learning.agent_runtime.model import OneShotChatPort
from knowpath_backend.learning.agent_runtime.runtime import WebAgentRuntime
from knowpath_backend.learning.rag.contracts import SourceSpan
from knowpath_backend.learning.rag.evidence import PreparedEvidence
from knowpath_backend.learning.rag.model_services import BudgetedJsonModel, _strict_json
from knowpath_backend.learning.rag.runtime import RequestTurnBudget
from knowpath_backend.learning.rag.scope import legacy_span, covers
from knowpath_backend.learning.rag.verification import AnswerVerifier, VerificationError


class _JournalPort:
    """One ledger entry and spending reservation per single-attempt model send."""
    def __init__(self, port, settings, turn_budget):
        self.port, self.settings, self.turn_budget = port, settings, turn_budget
        self.stage = 'generation'

    def complete(self, request, **kwargs):
        budget = self.turn_budget
        if time.monotonic() >= budget.deadline:
            raise VerificationError('RAG_DEADLINE_EXCEEDED')
        physical = httpx.Request('POST', self.settings.chat_base_url.rstrip('/') + '/chat/completions', json=request)
        budget.spending.reserve(physical)
        identifier = budget.journal.begin_call(self.stage)
        try:
            result = self.port.complete(request, **kwargs)
        except BaseException:
            budget.journal.finish_call(identifier, status='unknown')
            raise
        usage = getattr(result, 'usage', None)
        usage = usage.model_dump() if hasattr(usage, 'model_dump') else vars(usage) if usage is not None else {}
        budget.journal.finish_call(identifier, status='succeeded', usage=usage)
        return result


class SharedPortJsonModel(BudgetedJsonModel):
    """Use the ordinary v2 checker schema with the Agent's exact budget port."""
    allows_contract_retry = False

    def __init__(self, settings, model_port, physical_port, turn_budget):
        super().__init__(settings, max_input_tokens=settings.context_budget_tokens,
                         max_output_tokens=1024, journal=turn_budget.journal, stage='verification')
        self.model_port, self.physical_port = model_port, physical_port

    def generate_json(self, messages, *, deadline, response_schema=None):
        if time.monotonic() >= deadline:
            raise VerificationError('RAG_DEADLINE_EXCEEDED')
        self.last_usage = None
        body = self.request_body(messages, response_schema=response_schema)
        previous = self.physical_port.stage
        self.physical_port.stage = 'verification'
        try:
            response = self.model_port.complete(body, stream=False)
        finally:
            self.physical_port.stage = previous
        choice = response.choices[0]
        if choice.finish_reason != 'stop' or getattr(choice.message, 'tool_calls', None):
            raise VerificationError('MODEL_INVALID_RESPONSE')
        try:
            result = _strict_json(choice.message.content)
            if type(result) is not dict:
                raise ValueError()
        except (ValueError, TypeError):
            raise VerificationError('MODEL_INVALID_RESPONSE') from None
        usage = getattr(response, 'usage', None)
        self.last_usage = usage.model_dump() if hasattr(usage, 'model_dump') else None
        return result


def run_web_turn(service, message, session, policy, *, job=None, model_port=None,
                 on_event=None, should_cancel=None):
    """Adapt an authoritative accepted message without persisting its candidate.

    Injected ports have the same single-attempt contract as the factory port.
    The caller commits the result, complete dependency graph and memory session.
    """
    snapshot = deepcopy(message['snapshot'])
    receipt = snapshot['memory_scope']
    if receipt != policy.current:
        raise VerificationError('STALE_LEARNING_CONTEXT')
    settings = service.context_settings
    deadline = time.monotonic() + settings.chat_timeout_seconds
    pipeline = service.rag_pipeline
    turn_budget = (pipeline.new_turn_budget(deadline=deadline) if pipeline is not None
                   else RequestTurnBudget(deadline))
    accepted = {}

    def cancelled():
        if job is not None:
            job.check()
        return bool(should_cancel and should_cancel())

    raw_port = model_port if model_port is not None else OneShotChatPort(settings,
        deadline=turn_budget.deadline, should_cancel=cancelled)
    physical_port = _JournalPort(raw_port, settings, turn_budget)

    def guard():
        if cancelled():
            raise TurnCancelled('Web turn cancelled')
        if time.monotonic() >= turn_budget.deadline:
            raise VerificationError('RAG_DEADLINE_EXCEEDED')
        current = service.repository.get_record('messages', message['id'], lock=False)
        if current['snapshot'].get('memory_scope') != receipt:
            raise VerificationError('STALE_LEARNING_CONTEXT')
        space = service.spaces.repository.get(message['space_id'])
        error = service._context_error(space, current, snapshot)
        if error:
            raise VerificationError(error)
        return space

    def convert(source):
        """Upgrade only proven legacy coordinates, never fabricated retrieval IDs."""
        row = deepcopy(source)
        version = service.spaces.materials.get_version(row['material_version_id'])
        material = service.spaces.materials.get_material(row['material_id'])
        if (version is None or material is None or material.status == 'archived'
                or version.status != 'ready' or version.material_id != row['material_id']):
            raise VerificationError('RAG_SOURCE_INVALID')
        if row.get('citation_schema_version') == 2:
            if not row.get('source_spans') or not isinstance(row.get('source_text'), str):
                raise VerificationError('RAG_SOURCE_INVALID')
            for value in row['source_spans']:
                span = SourceSpan.model_validate(value)
                chunk = next((chunk for chunk in version.chunks if chunk.id == span.block), None)
                if chunk is None or not covers(span, [legacy_span(version.id, chunk)]):
                    raise VerificationError('RAG_SOURCE_INVALID')
            row['text'] = row['source_text']
            return row
        chunk = next((chunk for chunk in version.chunks if chunk.id == row['chunk_id']), None)
        if chunk is None or row.get('text') != chunk.text:
            raise VerificationError('RAG_SOURCE_INVALID')
        span = legacy_span(version.id, chunk)
        row.update(source_text=chunk.text, text=chunk.text, citation_schema_version=2,
                   source_spans=[span.model_dump()])
        return row

    def allowed_sources():
        # Acceptance already froze the reading/lesson/document intersection.
        # Recheck it against today's business scope before every use.
        guard()
        rows = snapshot.get('retrieval_sources', snapshot['sources'])
        if snapshot.get('reading_context') or snapshot.get('lesson_id'):
            keys = {(row['material_id'], row['material_version_id'], row['chunk_id']) for row in snapshot['sources']}
            rows = [row for row in rows if (row['material_id'], row['material_version_id'], row['chunk_id']) in keys]
        if snapshot.get('lesson_id'):
            keys = {tuple(key) for key in snapshot['lesson_source_keys']}
            rows = [row for row in rows if (row['material_id'], row['material_version_id'], row['chunk_id']) in keys]
        return [convert(row) for row in rows]

    def prepare(query):
        allowed = allowed_sources()
        if pipeline is not None:
            prepared = pipeline.prepare_evidence(query, space_id=message['space_id'],
                expected_scope_version=snapshot['scope_version'], expected_bindings=snapshot['bindings'],
                material_ids=snapshot.get('request_scope', {}).get('material_ids'),
                cancelled=cancelled, history=deepcopy(snapshot.get('history', [])),
                turn_budget=turn_budget, allowed_sources=allowed)
            # Registry originals retain retrieval identities; compare each source
            # against the persisted immutable retrieval chunk, never model data.
            normalized = []
            for row in prepared.sources:
                stored = pipeline.repo.get_chunk(row['retrieval_version_id'], row['chunk_id'])
                if stored is None or any(row.get(key) != stored.get(key) for key in
                        ('material_version_id', 'source_text', 'source_spans')):
                    raise VerificationError('RAG_SOURCE_INVALID')
                normalized.append(convert(row))
            prepared = replace(prepared, sources=normalized)
        else:
            turn_budget.admit()
            try:
                original_rows = snapshot.get('retrieval_sources', snapshot['sources'])
                allowed_keys = {(row['material_id'], row['material_version_id'], row['chunk_id']) for row in allowed}
                original_rows = [row for row in original_rows if (row['material_id'], row['material_version_id'], row['chunk_id']) in allowed_keys]
                selected = service.retriever.select(query, deepcopy(original_rows), limit=5) if original_rows else []
                if (not isinstance(selected, list) or len(selected) > 5 or any(row not in original_rows for row in selected)
                        or len({row['chunk_id'] for row in selected}) != len(selected)):
                    raise VerificationError('RAG_SOURCE_INVALID')
                rows = [convert(row) for row in selected]
                prepared = PreparedEvidence('ready', query, rows,
                    {'generation_calls': 0, 'verification_calls': 0}, None, turn_budget.deadline, time.monotonic())
            finally:
                turn_budget._active.release()
        guard()
        for row in prepared.sources:
            accepted[row['chunk_id']] = deepcopy(row)
        return prepared

    def verify(question, sources, candidate, *, deadline, model_port):
        allowed = allowed_sources()
        allowed_spans = [SourceSpan.model_validate(span) for row in allowed for span in row['source_spans']]
        for row in sources:
            if (row != accepted.get(row['chunk_id']) or any(
                    not covers(SourceSpan.model_validate(span), allowed_spans) for span in row['source_spans'])):
                raise VerificationError('RAG_SOURCE_INVALID')
            convert(row)
        turn_budget.admit()
        try:
            checker = SharedPortJsonModel(settings, model_port, physical_port, turn_budget)
            result = AnswerVerifier(checker, checker).verify_candidate(question, deepcopy(sources), candidate,
                deadline=min(deadline, turn_budget.deadline))
            guard()
            return result
        finally:
            turn_budget._active.release()

    request = TurnRequest(message_id=message['id'], run_id=message['run_id'], space_id=message['space_id'],
        conversation_id=message['conversation_id'], input_text=message['message'],
        answer_mode=snapshot.get('requested_answer_mode', snapshot.get('answer_mode', 'auto')),
        scope_fingerprint=receipt['fingerprint'], context_fingerprint=receipt['fingerprint'],
        context_epoch=receipt['context_epoch'], space_sequence=receipt['receive_sequence'],
        material_bindings=deepcopy(snapshot['bindings']), budget=TurnBudgetLimits(
            max_model_calls=8, max_tool_rounds=6, max_input_tokens=128000, max_output_tokens=8192))
    try:
        space = guard()
        current_topics = {topic['id'] for topic in service.spaces.bound_topics(space)
                          if topic['id'] not in set(space.get('excluded_topic_ids', []))
                          and (space.get('scope_mode') == 'all' or not space.get('topic_ids') or topic['id'] in space['topic_ids'])}
        allowed_topics = sorted(current_topics & {topic['id'] for topic in snapshot.get('route_topics', [])})
        result = WebAgentRuntime(settings, model_port=physical_port).run(request, memory=session.manager,
            policy=policy, prepare_evidence=prepare, verify_candidate=verify,
            on_event=on_event, should_cancel=cancelled, allowed_topic_ids=allowed_topics)
        return replace(result, usage={**result.usage, 'call_journal': turn_budget.finish(),
                                                 'spending_budget': turn_budget.spending.trace()})
    except BaseException as error:
        error.call_journal = turn_budget.finish('failed')
        raise
