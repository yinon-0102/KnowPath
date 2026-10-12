"""Durable source-bound messages; model I/O occurs outside database locks."""
import copy
import re
import logging
from knowpath_backend.observability import log_event, span

from uuid import uuid4

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.conversations.schemas import SendMessage
from knowpath_backend.learning.conversations.document_scope import resolve_document_scope, resolve_reading_scope, filter_document_sources
from knowpath_backend.learning.conversations.generation import DashScopeAnswerGenerator, MessageGenerationError, validate_answer
from knowpath_backend.learning.workers.runs import ACTIVE_STATUSES
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker, TRANSIENT_ERRORS, LeaseLost, enqueue
from knowpath_backend.learning.spaces.service import SpaceService, now
from knowpath_backend.learning.rag.retrieval import KeywordRetriever, RetrievalError, configured_retriever
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.conversations.context import bound_snapshot, project_memory, delivery_source_refs, message_source_refs
from knowpath_backend.learning.rag.verification import VerificationError, safe_error_details
from knowpath_backend.learning.materials.source_access import SourceAccessService, AssistanceDeliveryChanged

logger = logging.getLogger(__name__)


def uid():
    return str(uuid4())


class MessageService:
    def __init__(self, repository, spaces, assessments, runs, generator=None, retriever=None, context_settings=None, rag_pipeline=None):
        self.repository, self.spaces, self.assessments, self.runs = repository, spaces, assessments, runs
        self.generator = generator if generator is not None else DashScopeAnswerGenerator()
        self.commands = SpaceService(repository, spaces.materials)
        self.retriever = retriever if retriever is not None else configured_retriever()
        self.context_settings = context_settings or getattr(self.generator, "settings", None) or LearningSettings.from_env()
        self.rag_pipeline = rag_pipeline
        self.source_access = SourceAccessService(assessments)

    def send(self, space_id, payload, key=None, *, dispatch=None, durable=False):
        payload = SendMessage.model_validate(payload).model_dump()
        # Preserve hashes of pre-selection idempotent requests.
        if payload.get('material_ids') is None:
            payload.pop('material_ids', None)
        if payload.get('lesson_id') is None:
            payload.pop('lesson_id', None)
        if payload.get('reading_context') is None:
            payload.pop('reading_context', None)
        requested_answer_mode = payload.get("answer_mode", "auto")
        if requested_answer_mode == "auto":
            payload.pop("answer_mode", None)
        created = []
        def prepare():
            # Hint requests must serialize with grading before taking space locks.
            from knowpath_backend.learning.assessments.adaptive import reached_questions
            assessments = self.repository.records("assessments", space_id=space_id)
            space = self.spaces.repository.get(space_id)
            reading = payload.get('reading_context')
            try:
                request_scope = (resolve_reading_scope(space, reading, payload.get('material_ids')) if reading
                                 else resolve_document_scope(space, payload.get('material_ids')))
            except DomainConflict as exc:
                log_event(logger, 'message.scope.rejected', space_id=space_id, error_code=exc.code)
                raise
            sources = (self._reading_sources(space, reading) if reading
                       else filter_document_sources(self._sources(space), request_scope))
            conversation = self._conversation(space_id, payload["session_id"])
            previous = sorted(self.repository.records("messages", conversation_id=conversation["id"]), key=lambda m: m["sequence"])
            last = previous[-1]['snapshot'] if previous else {}
            previous_reading = last.get('reading_context')
            if ((previous_reading and reading != previous_reading)
                    or (reading and previous and previous_reading != reading)
                    or (reading and conversation.get('lesson_id'))):
                raise DomainConflict('READING_CONTEXT_MISMATCH', '请为当前原文新建独立问答会话')
            selected_lesson = None if reading else payload.get('lesson_id') or conversation.get('lesson_id')
            lesson = None
            if selected_lesson:
                if space['status']=='archived':raise DomainConflict('SPACE_ARCHIVED','归档空间不能发送课件追问')
                lesson = self.repository.get_record('study_lessons',selected_lesson)
                if lesson['space_id'] != space_id or lesson['status'] != 'completed' or self.lessons._stale(lesson):
                    raise DomainConflict('STALE_LEARNING_CONTEXT','请选择当前节点的有效课件')
                allowed={(s['material_id'],s['material_version_id'],s['chunk_id']) for s in lesson['snapshot']['sources']}
                sources=[s for s in sources if (s['material_id'],s['material_version_id'],s['chunk_id']) in allowed]
                conversation['lesson_id']=selected_lesson
                self.repository.put_record('conversations',conversation)
            if not sources and requested_answer_mode == "material":
                raise DomainConflict("NO_LEARNING_SOURCES", "当前学习范围没有可引用的资料")
            for message in previous:
                if message["status"] in {"pending", "generating"} and self.runs.get(message["run_id"])["status"] in ACTIVE_STATUSES:
                    raise DomainConflict("MESSAGE_IN_PROGRESS", "该会话已有生成中的消息")
            context = project_memory(self.repository.records("messages", space_id=space_id),
                                     space, conversation["id"], payload["message"], request_scope=request_scope,
                                     reading_context=reading)
            if lesson:
                # Scope changes discard previous/global prose as well as retrieval
                # candidates: otherwise memory can disclose other node sources.
                safe=[]
                for prior in previous:
                    snap=prior['snapshot']
                    if prior['status']!='completed' or not prior.get('response') or snap.get('lesson_id')!=lesson['id'] or snap.get('lesson_context_fingerprint')!=lesson['context_fingerprint'] or snap.get('request_scope',{}).get('request_scope_id')!=request_scope['request_scope_id']:continue
                    refs=message_source_refs(prior,previous)
                    if filter_document_sources(refs,request_scope)==refs and all((r['material_id'],r['material_version_id'],r['chunk_id']) in allowed for r in refs):safe.append((prior,refs))
                preceding=[m for m in previous if m['status']=='completed' and m.get('response')]
                latest=preceding[-1] if preceding else None
                if latest and latest['id'] not in {m['id'] for m,refs in safe}:safe=[]
                safe=safe[-5:]
                context={'history':[p for prior,refs in safe for p in ({'role':'user','content':prior['message']},{'role':'assistant','content':prior['response']['text']})], 'memory':{'summary':{},'recall':[]},'context_provenance':{'history':[refs for prior,refs in safe],'summary':[],'recall':{}}}
                # Structured lesson prose is untrusted context. Carry exact
                # original source provenance even if retrieval selects fewer.
                selected_sources=filter_document_sources(lesson['snapshot']['sources'],request_scope)
                # Model prose can depend on every source it saw, even when it
                # cites only one. A narrower request cannot reuse mixed prose.
                lecture=('\n\n'.join(s['kind']+'\n'+s['markdown'] for s in lesson['sections'])[:8000]
                         if selected_sources==lesson['snapshot']['sources'] else '')
                if lecture:
                    context['memory']['summary']={'text':lecture,'method':'lesson_excerpt','style':lesson['style']}
                    context['context_provenance']['summary']=copy.deepcopy(selected_sources)
            hint = self._hint(assessments, payload["message"])
            run = self.runs.create("message", status="queued" if durable else "running")
            identifier = uid()
            snapshot = {"message": payload["message"], "sources": sources, **context,
                        "scope_version": space["scope_version"], "bindings": copy.deepcopy(space["bindings"]), "hint": hint,
                        "request_scope": request_scope, "requested_answer_mode": requested_answer_mode,
                        "route_topics": [{"id": t["id"], "name": t["name"]} for t in self.spaces.bound_topics(space)],
                        "route_assessments": [{"id": a["id"], "questions": [{"topic_id": q.get("topic_id"), "prompt": q.get("prompt")} for q in (reached_questions(a) if a["status"] != "generating" else [])]} for a in assessments if a["status"] in {"generating", "ready", "in_progress"}]}
            if lesson:
                snapshot.update(lesson_id=lesson['id'],lesson_context_fingerprint=lesson['context_fingerprint'],lesson_source_keys=[list(k) for k in sorted(allowed)])
            if reading:
                snapshot['reading_context'] = copy.deepcopy(reading)
            record = {"id": identifier, "space_id": space_id, "conversation_id": conversation["id"],
                      "run_id": run["id"], "status": "pending", "message": payload["message"],
                      "sequence": len(previous) + 1, "snapshot": snapshot, "response": None, "created_at": now()}
            self.repository.put_record("messages", record)
            log_event(logger, 'message.scope.frozen', message_id=identifier, run_id=run['id'], space_id=space_id,
                      request_scope_id=request_scope['request_scope_id'], material_ids=request_scope['material_ids'],
                      source_count=len(sources))
            event_id = enqueue(self.repository, "message", record) if durable else None
            created.append((identifier, run["id"], event_id))
            return {"run_id": run["id"], "session_id": conversation["id"], "status": "queued" if durable else "running"}
        response = self.commands._execute("message.send", space_id, payload, key, prepare)
        # A losing same-key transaction can leave a local preparation record.
        # Only dispatch the preparation whose Run was actually committed.
        if created and created[-1][1] == response["run_id"]:
            if durable:
                if dispatch is not None:
                    dispatch(ModelTaskWorker(messages=self).run_once, created[-1][2])
            elif dispatch is None:
                self.generate(created[-1][0])
            else:
                dispatch(self.generate, created[-1][0])
        return response

    def _conversation(self, space_id, session_id):
        if session_id:
            try:
                conversation = self.repository.get_record("conversations", session_id)
            except DomainNotFound:
                learning = self.repository.get_record("sessions", session_id)
                if learning["space_id"] != space_id:
                    raise DomainNotFound("session", session_id)
                if learning["status"] != "active":
                    raise DomainConflict("SESSION_FINISHED", "学习会话已经结束")
                conversation = {"id": session_id, "space_id": space_id, "learning_session_id": session_id, "created_at": now()}
                self.repository.put_record("conversations", conversation)
            if conversation["space_id"] != space_id:
                raise DomainNotFound("session", session_id)
            previous=self.repository.records('messages',conversation_id=conversation['id'])
            last= max(previous,key=lambda m:m['sequence'],default=None)
            if last and last['snapshot'].get('lesson_id'):
                conversation['lesson_id']=last['snapshot']['lesson_id']
            if conversation.get("learning_session_id"):
                learning = self.repository.get_record("sessions", conversation["learning_session_id"])
                if learning["status"] != "active":
                    if conversation.get('lesson_id'):
                        conversation['learning_session_id']=None
                        self.repository.put_record('conversations',conversation)
                    else: raise DomainConflict("SESSION_FINISHED", "学习会话已经结束")
            return conversation
        conversation = {"id": uid(), "space_id": space_id, "learning_session_id": None, "created_at": now()}
        self.repository.put_record("conversations", conversation)
        return conversation

    def _sources(self, space):
        selected, excluded = set(space["topic_ids"]), set(space["excluded_topic_ids"])
        sources, versions = {}, {}
        for topic in self.spaces.bound_topics(space):
            if (selected and topic["id"] not in selected) or topic["id"] in excluded:
                continue
            for ref in topic["source_refs"]:
                version_id = ref["material_version_id"]
                if version_id not in versions:
                    versions[version_id] = self.spaces.materials.get_version(version_id)
                version = versions[version_id]
                if version is None or version.material_id != ref["material_id"]:
                    raise DomainConflict("BOUND_VERSION_UNAVAILABLE", "绑定资料版本不可用")
                chunk = next((c for c in version.chunks if c.id == ref["chunk_id"]), None)
                if chunk is not None:
                    sources[chunk.id] = {**ref, "topic_id": topic["id"], "topic_name": topic["name"], "text": chunk.text,
                                         "graph_version": topic["graph_version"]}
        return list(sources.values())

    def _reading_sources(self, space, reading):
        """All available original chunks, bounded by the space's topic policy.

        Unmapped chunks are available only without an explicit topic whitelist.
        An exclusion always wins, including chunks shared by multiple topics.
        """
        resolve_reading_scope(space, reading)
        material = self.spaces.materials.get_material(reading['material_id'])
        version = self.spaces.materials.get_version(reading['material_version_id'])
        if (material is None or material.status == 'archived' or version is None
                or version.status != 'ready' or version.material_id != reading['material_id']):
            raise DomainConflict('BOUND_VERSION_UNAVAILABLE', '绑定原文已删除或不可用')
        topics = self.spaces.bound_topics(space)
        aliases = {t['id']: t.get('canonical_topic_id', t['id']) for t in topics}
        selected = {aliases.get(t, t) for t in space['topic_ids']}
        excluded = {aliases.get(t, t) for t in space['excluded_topic_ids']}
        allowed, denied = {}, set()
        binding = next(b for b in space['bindings'] if b['material_id'] == reading['material_id'])
        for topic in topics:
            canonical = aliases[topic['id']]
            for ref in topic['source_refs']:
                if ref['material_id'] != reading['material_id'] or ref['material_version_id'] != version.id:
                    continue
                if canonical in excluded:
                    denied.add(ref['chunk_id'])
                elif not selected or canonical in selected:
                    allowed[ref['chunk_id']] = {**ref, 'topic_id': topic['id'], 'topic_name': topic['name']}
        sources = []
        for chunk in version.chunks:
            if chunk.id in denied or (selected and chunk.id not in allowed):
                continue
            sources.append({**allowed.get(chunk.id, {}), 'material_id': material.id,
                'material_version_id': version.id, 'chunk_id': chunk.id,
                'graph_version': binding['graph_version'], 'text': chunk.text,
                'section_path': list(chunk.section_path), 'line_start': chunk.line_start,
                'line_end': chunk.line_end, 'page': chunk.page})
        return sources

    def _hint(self, assessments, message):
        active = [a for a in assessments if a["status"] in {"ready", "in_progress"}]
        normalized = message.casefold()
        asking = bool(re.search(
            r"答案|选哪个|选什么|怎么选|正确选项|提示|解答|(?:帮我|替我|完成|做完).{0,40}(?:题|测验|测试|试卷)|"
            r"\b(?:answers?|solutions?|hints?)\b|"
            r"\b(?:solve|complete|finish)\b.{0,80}\b(?:quiz|test|exam|questions?|assessment)\b|"
            r"\bdo\s+(?:(?:this|the|my|these)\s+)?(?:quiz|test|exam|questions?|assessment)\b|"
            r"\b(?:which|choose|select|pick|correct)\b.{0,80}\b(?:options?|choices?|letters?)\b",
            normalized, re.DOTALL))
        matched = [(a, q) for a in active for q in a["questions"]
                   if q["id"] in message or q["prompt"].casefold() in normalized]
        if not matched and asking:
            matched = [(a, q) for a in active for q in a["questions"]]
        # Wording chooses the deterministic response only. Assistance is
        # source-bound and recorded atomically when help is actually delivered.
        return bool(matched)

    def read_run(self, run_id, *, after_id=0, include_run=False):
        """Audit historical help again before returning an event-bearing read.

        Event cursors and tombstones are resolved before preparing assistance.
        The returned events are reread with the message and source aggregates
        locked, so deletion or finalization cannot expose an unaudited answer.
        """
        def read():
            return self.runs.get(run_id) if include_run else self.runs.events_for(run_id, after_id=after_id)
        def has_help(value):
            events = value["events"] if include_run else value
            def disclosed(event):
                data = event.get("data") or {}
                if event["event"] == "message.completed":
                    return bool(data.get("text"))
                return event["event"] == "message.delta" and bool(data.get("delta"))
            return any(disclosed(event) for event in events)
        def sources(message):
            snapshot = message["snapshot"]
            originals = (self.repository.records("messages", space_id=message["space_id"], lock=False)
                         if not {"delivered_source_refs", "context_provenance"} & snapshot.keys() else ())
            return message_source_refs(message, originals)
        delivery_id = uid()
        for _ in range(3):
            value = read()
            if not has_help(value):
                return value
            rows = self.repository.records("messages", run_id=run_id, lock=False)
            if not rows:
                raise DomainConflict("MESSAGE_CONTEXT_UNAVAILABLE", "历史回答来源不可用，无法安全重放")
            initial = rows[0]
            refs = sources(initial)
            # A verified RAG clarification/refusal can intentionally disclose
            # no source facts. Its persisted empty audit is different from
            # unknown provenance on an older or erased response.
            if not refs and "delivered_source_refs" not in initial["snapshot"]:
                raise DomainConflict("MESSAGE_CONTEXT_UNAVAILABLE", "历史回答来源不可用，无法安全重放")
            plan = self.source_access.prepare_delivery(refs, initial["space_id"], assistance_scope=self._assistance_scope(initial["snapshot"]))
            def deliver():
                active, _ = self.source_access.lock_delivery(plan, initial["space_id"])
                current = self.repository.get_record("messages", initial["id"])
                fresh = read()
                if has_help(fresh):
                    current_refs = sources(current)
                    if (current["status"] not in {'generating','completed'} or current_refs != refs
                            or (not current_refs and "delivered_source_refs" not in current["snapshot"])):
                        raise AssistanceDeliveryChanged()
                    if current['status']=='generating':
                        run=self.runs.get(run_id)
                        if run['status']!='running':
                            self.runs.withdraw_drafts(run_id);return read()
                        from datetime import datetime,timezone
                        from knowpath_backend.learning.workers.model_tasks import timestamp
                        jobs=[event for event in self.repository.records('outbox',aggregate_id=current['id']) if event['event_type']=='message.generate' and event['payload'].get('run_id')==run_id]
                        if jobs and not any(event['status']=='processing' and event.get('lease_until') and timestamp(event['lease_until'])>datetime.now(timezone.utc) for event in jobs):
                            self.runs.withdraw_drafts(run_id);return read()
                        space=self.spaces.repository.get(initial['space_id'])
                        error=self._context_error(space,current,current['snapshot'])
                        if error:raise DomainConflict(error,'回答生成范围已变化')
                    self.source_access.record_delivery(active, plan, kind="message_replay",
                        delivery_id=delivery_id, message_id=current["id"], space_id=current["space_id"])
                return fresh
            try:
                return self.commands._execute("message.replay", initial["space_id"], {}, None, deliver)
            except AssistanceDeliveryChanged:
                continue
        raise DomainConflict("STALE_LEARNING_CONTEXT", "学习范围已变化，请重新读取消息")

    def generate(self, identifier, *, job=None):
        try:
            initial = self.repository.get_record("messages", identifier, lock=False)
            with self.repository.transaction():
                self.spaces.repository.get(initial["space_id"])
                current = self.repository.get_record("messages", identifier)
                if job is not None:
                    job.check()
                if current["status"] not in ({"pending", "generating"} if job else {"pending"}):
                    return
                if self._terminal(current):
                    return
                interrupted = current["snapshot"].get("rag_execution_started") is True
                if interrupted or (self.rag_pipeline is not None and "requested_answer_mode" not in current["snapshot"] and not current["snapshot"].get('lesson_id') and not current['snapshot'].get('reading_context') and not current["snapshot"]["hint"]):
                    # Commit the fence before any external call. A crashed
                    # attempt may have consumed its provider budget even when
                    # no result was saved; replay must fail closed, including
                    # after deployment changes the configured pipeline.
                    current["snapshot"] = {**current["snapshot"], "rag_mode": True,
                                           "rag_execution_started": True}
                current["status"] = "generating"
                self.repository.put_record("messages", current)
        except DomainNotFound:
            return
        snapshot = current["snapshot"]
        if interrupted:
            self._publish_answer(initial, identifier, snapshot, None, None,
                                 "RAG_EXECUTION_INTERRUPTED", None, job=job)
            return
        error = None
        error_details = {}
        answer_status = None
        try:
            if "requested_answer_mode" in snapshot:
                text, citations, snapshot = self._adaptive_answer(identifier, snapshot, current, job=job)
                if text is None:
                    return
                answer_status = snapshot.get("query_status")
            elif self.rag_pipeline is not None and not snapshot.get('lesson_id') and not snapshot.get('reading_context') and not snapshot["hint"]:
                def cancelled():
                    if job is not None:
                        job.check()
                    return self.runs.get(current["run_id"])["status"] not in {"queued", "running"}
                result = self.rag_pipeline.answer(snapshot["message"], space_id=current["space_id"],
                    expected_scope_version=snapshot["scope_version"], expected_bindings=snapshot["bindings"],
                    cancelled=cancelled, history=copy.deepcopy(snapshot.get('history', [])),
                    **({'material_ids': snapshot['request_scope']['material_ids']}
                       if snapshot.get('request_scope', {}).get('mode') == 'selected' else {}))
                snapshot = {**snapshot, "sources": result["sources"], "rag_trace": result["trace"]}
                if not self._record_sources(identifier, snapshot, job=job):
                    return
                text, citations, answer_status = result["text"], result["citations"], result["status"]
            else:
                on_delta = lambda delta: self._publish_delta(identifier,current['run_id'],delta,job=job)
                text, citations, snapshot = self._legacy_answer(identifier, snapshot, job=job, on_delta=on_delta)
                if text is None:
                    return
                answer_status = snapshot.get('query_status')
        except LeaseLost:
            raise
        except VerificationError as exc:
            log_event(logger, 'message.generation.failed', level=logging.WARNING, exc=exc, error_code=exc.code,
                      message_id=identifier, run_id=current['run_id'], space_id=current['space_id'])
            error = exc.code
            error_details = safe_error_details(exc.details)
            from knowpath_backend.learning.rag.diagnostics import safe_journal_snapshot
            snapshot['rag_failure_journal'] = safe_journal_snapshot(getattr(exc, 'call_journal', None))
        except (MessageGenerationError, RetrievalError) as exc:
            log_event(logger, 'message.generation.failed', level=logging.WARNING, exc=exc, error_code=exc.code,
                      message_id=identifier, run_id=current['run_id'], space_id=current['space_id'])
            error = exc.code
        except Exception as exc:
            log_event(logger, 'message.generation.failed', level=logging.ERROR, exc=exc, error_code='MODEL_UNAVAILABLE',
                      message_id=identifier, run_id=current['run_id'], space_id=current['space_id'])
            # Custom providers must not leak request/credential details.
            error = "MODEL_UNAVAILABLE"
        if error is not None:
            try:
                persisted = self.repository.get_record("messages", identifier, lock=False)["snapshot"]
                snapshot = {**persisted, **({"rag_failure_journal": snapshot["rag_failure_journal"]} if snapshot.get("rag_failure_journal") else {})}
            except DomainNotFound:
                return
        self._publish_answer(initial, identifier, snapshot, text if error is None else None,
                             citations if error is None else None, error, answer_status,
                             error_details=error_details, job=job)

    @staticmethod
    def _assistance_scope(snapshot):
        from .adaptive import assistance_scope
        return assistance_scope(snapshot)

    def _page_assistance(self, rows):
        scopes=[self._assistance_scope(r['snapshot']) for r in rows if r.get('response')]
        scopes=[scope for scope in scopes if scope is not None]
        if not scopes:return None
        return {'topic_ids': sorted({t for scope in scopes for t in scope['topic_ids']}),
                'all_topics': any(scope['all_topics'] for scope in scopes)}

    def _adaptive_answer(self, identifier, snapshot, current, *, job=None):
        from .adaptive import validate_route, validate_adaptive_answer
        if not snapshot.get('semantic_route'):
            classifier=getattr(self.generator, 'classify', None)
            if classifier is None:
                classifier = DashScopeAnswerGenerator(self.context_settings).classify
            route=validate_route(classifier(copy.deepcopy(snapshot)),snapshot)
            requested=snapshot['requested_answer_mode']
            mode='material' if requested=='material' or (requested=='auto' and route['document_related']) else 'general'
            snapshot={**snapshot, 'semantic_route':copy.deepcopy(route), 'answer_mode':mode, 'hint':False}
            if not self._record_sources(identifier,snapshot,job=job,publish_sources=False):return None,None,snapshot
        if snapshot.get('context_scope_changed'):
            from knowpath_backend.learning.rag.queries import prepare_query
            if prepare_query(snapshot['message'],snapshot.get('history',[]))['status']=='clarify':
                snapshot={**snapshot,'sources':[],'query_status':'clarify','history':[],
                          'memory':{'summary':{},'recall':[]},
                          'context_provenance':{'history':[],'summary':[],'recall':{}}}
                if not self._record_sources(identifier,snapshot,job=job):return None,None,snapshot
                return '请明确本轮问题指向的对象或资料范围。',[],snapshot
        if snapshot['answer_mode']=='general':
            snapshot={**snapshot,'sources':[], 'retrieval_sources':[]}
            snapshot['memory']={'summary':{},'recall':[]}
            snapshot['context_provenance']={**snapshot.get('context_provenance',{}),'summary':[],'recall':{}}
            route=snapshot['semantic_route']
            if not route['educational'] or (not route['topic_ids'] and not route['scope_uncertain']):
                # Source-backed history is irrelevant to a confidently unrelated turn.
                # Remove it from answer input, so its sources do not taint smalltalk audits.
                turns=[];provenance=[]
                history=snapshot.get('history',[])
                for pos in range(0,len(history),2):
                    refs=snapshot['context_provenance'].get('history',[])[pos//2:pos//2+1]
                    if any(refs):continue
                    turns.extend(history[pos:pos+2]);provenance.extend(refs or [[]])
                snapshot['history']=turns;snapshot['context_provenance']['history']=provenance
        elif self.rag_pipeline is not None and not snapshot.get('lesson_id') and not snapshot.get('reading_context'):
            snapshot={**snapshot,'rag_mode':True,'rag_execution_started':True}
            if not self._record_sources(identifier,snapshot,job=job,publish_sources=False):return None,None,snapshot
            def cancelled():
                if job is not None:job.check()
                return self.runs.get(current['run_id'])['status'] not in {'queued','running'}
            result=self.rag_pipeline.answer(snapshot['message'],space_id=current['space_id'],
                expected_scope_version=snapshot['scope_version'],expected_bindings=snapshot['bindings'],
                cancelled=cancelled,history=copy.deepcopy(snapshot.get('history',[])),
                **({'material_ids':snapshot['request_scope']['material_ids']}
                   if snapshot.get('request_scope',{}).get('mode')=='selected' else {}))
            snapshot={**snapshot,'sources':result['sources'],'rag_trace':result['trace'],'query_status':result['status']}
            if not self._record_sources(identifier,snapshot,job=job):return None,None,snapshot
            if result['status'] not in {'partial','insufficient'} or snapshot['requested_answer_mode']=='material':
                return result['text'],result['citations'],snapshot
            if result['status']=='partial':
                snapshot={**snapshot,'grounded_answer':result['text'],'grounded_citation_ids':[s['chunk_id'] for s in result['citations']],
                          'grounded_citations':result['citations'],'answer_mode':'mixed',
                          'supplement_explanation':'当前资料覆盖了部分问题；额外的通用知识将单独标注。'}
            else:
                snapshot={**snapshot,'sources':[],'answer_mode':'general',
                          'supplement_explanation':'当前资料未覆盖此问题；此回答采用模型通用知识。'}
        else:
            candidates=snapshot.get('retrieval_sources',snapshot['sources'])
            if candidates:
                try:selected=self.retriever.select(snapshot['message'],copy.deepcopy(candidates),limit=8)
                except RetrievalError:raise
                except Exception:raise RetrievalError('VECTOR_UNAVAILABLE') from None
                if (not isinstance(selected,list) or len(selected)>8 or any(s not in candidates for s in selected)
                    or len({s['chunk_id'] for s in selected})!=len(selected)):
                    raise RetrievalError('RETRIEVAL_VALIDATION_FAILED')
            else:selected=[]
            snapshot={**snapshot,'sources':copy.deepcopy(selected),'retrieval_sources':candidates}
            if not selected and snapshot['requested_answer_mode']=='auto':
                snapshot['answer_mode']='general'
                snapshot['supplement_explanation']='当前资料未覆盖此问题；此回答采用模型通用知识。'
        snapshot=bound_snapshot(snapshot,budget=self.context_settings.context_budget_tokens)
        if not self._record_sources(identifier,snapshot,job=job):return None,None,snapshot
        from time import monotonic
        pending=[]; pending_chars=0; last_flush=None
        def flush():
            nonlocal pending_chars,last_flush
            if pending:
                self._publish_delta(identifier,current['run_id'],''.join(pending),job=job)
                pending.clear();pending_chars=0;last_flush=monotonic()
        def on_text(delta):
            nonlocal pending_chars
            if not isinstance(delta,str) or not delta:return
            pending.append(delta);pending_chars+=len(delta)
            if last_flush is None or pending_chars>=128 or monotonic()-last_flush>=0.08:flush()
        if snapshot.get('grounded_answer'):
            # Application-owned attribution precedes every mixed stream fragment.
            on_text('### 资料依据\n\n'+snapshot['grounded_answer'].strip()+'\n\n### 通用知识补充\n\n')
            flush()
            if hasattr(self.generator,'stream') and 'supplement_text' in getattr(self.generator,'stream_fields',()):
                raw=self.generator.stream(copy.deepcopy(snapshot),on_text=on_text,text_field='supplement_text')
                flush()
            else:
                raw=(self.generator.stream(copy.deepcopy(snapshot),on_text=None) if hasattr(self.generator,'stream')
                     else self.generator.generate(copy.deepcopy(snapshot)))
                if isinstance(raw,dict) and isinstance(raw.get('supplement_text'),str):
                    on_text(raw['supplement_text']);flush()
        elif snapshot['answer_mode']=='material':
            if hasattr(self.generator,'stream') and getattr(self.generator,'section_streaming',False):
                section_state={'grounded_started':False,'grounded_complete':False,
                               'supplement_started':False,'buffer':[]}
                def supplemental(delta):
                    if not section_state['supplement_started']:
                        on_text(('\n\n' if section_state['grounded_started'] else '')+'### 通用知识补充\n\n')
                        section_state['supplement_started']=True
                    on_text(delta)
                def on_section(field,delta):
                    if field=='grounded_text':
                        if not section_state['grounded_started']:
                            on_text('### 资料依据\n\n');section_state['grounded_started']=True
                        on_text(delta)
                    elif field=='grounded_complete':
                        section_state['grounded_complete']=True
                        if section_state['buffer']:
                            supplemental(''.join(section_state['buffer']));section_state['buffer'].clear()
                    elif field=='supplement_text':
                        # JSON object key order is unconstrained. Supplement-first
                        # fragments wait for the grounded string to finish, then
                        # enter their explicitly labelled canonical section.
                        if section_state['grounded_complete']:supplemental(delta)
                        else:section_state['buffer'].append(delta)
                raw=self.generator.stream(copy.deepcopy(snapshot),on_text=None,on_section=on_section)
                on_section('grounded_complete','')
                flush()
                if not section_state['grounded_started'] and not section_state['supplement_started']:
                    validated=validate_adaptive_answer(raw,snapshot)[0]
                    on_text(validated);flush()
            else:
                raw=(self.generator.stream(copy.deepcopy(snapshot),on_text=lambda delta:None) if hasattr(self.generator,'stream')
                     else self.generator.generate(copy.deepcopy(snapshot)))
                validated=validate_adaptive_answer(raw,snapshot)[0]
                on_text(validated);flush()
        else:
            raw=(self.generator.stream(copy.deepcopy(snapshot),on_text=on_text) if hasattr(self.generator,'stream')
                 else self.generator.generate(copy.deepcopy(snapshot)))
            flush()
        if snapshot.get('grounded_answer'):
            if not isinstance(raw,dict) or not raw.get('supplement_text'):
                raise MessageGenerationError('MESSAGE_VALIDATION_FAILED')
            attempted=raw.get('citation_ids',[])
            if not isinstance(attempted,list) or any(not isinstance(i,str) or i not in {s['chunk_id'] for s in snapshot['sources']} for i in attempted):
                raise MessageGenerationError('MESSAGE_VALIDATION_FAILED')
            # Retain verified pipeline prose and citations, never ask the supplement
            # provider to regenerate or accurately copy already verified claims.
            raw={**raw,'grounded_text':snapshot['grounded_answer'],'citation_ids':snapshot['grounded_citation_ids']}
        text,citations,mode,explanation=validate_adaptive_answer(raw,snapshot)
        snapshot={**snapshot,'answer_mode':mode,'supplement_explanation':explanation or snapshot.get('supplement_explanation')}
        if not self._record_sources(identifier,snapshot,job=job):return None,None,snapshot
        return text,citations,snapshot

    def _legacy_answer(self, identifier, snapshot, *, job=None, on_delta=None):
        if (snapshot.get('request_scope', {}).get('mode') == 'selected' or snapshot.get('context_scope_changed')) and not snapshot['hint']:
            from knowpath_backend.learning.rag.queries import prepare_query
            prepared = prepare_query(snapshot['message'], snapshot.get('history', []))
            if prepared['status'] == 'clarify':
                snapshot = {**snapshot, 'sources': [], 'query_status': 'clarify', 'history': [],
                            'memory': {'summary': {}, 'recall': []},
                            'context_provenance': {'history': [], 'summary': [], 'recall': {}}}
                if not self._record_sources(identifier, snapshot, job=job):
                    return None, None, snapshot
                return '请明确本轮问题指向的对象或资料范围。', [], snapshot
        # Active assessment hints remain deterministic and require no provider.
        retriever = KeywordRetriever() if snapshot["hint"] else self.retriever
        retrieval_sources = snapshot.get("retrieval_sources", snapshot["sources"])
        try:
            with span(logger, 'message.retrieve', message_id=identifier, candidate_count=len(retrieval_sources)) as metrics:
                selected = retriever.select(snapshot["message"], copy.deepcopy(retrieval_sources), limit=8)
                if isinstance(selected, list):
                    metrics['context_count'] = len(selected)
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError("VECTOR_UNAVAILABLE") from None
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 8
                or any(row not in retrieval_sources for row in selected)
                or len({row["chunk_id"] for row in selected}) != len(selected)):
            raise RetrievalError("RETRIEVAL_VALIDATION_FAILED")
        snapshot = {**snapshot, "sources": copy.deepcopy(selected), "retrieval_sources": retrieval_sources}
        snapshot = bound_snapshot(snapshot, budget=self.context_settings.context_budget_tokens)
        if not self._record_sources(identifier, snapshot, job=job):
            return None, None, snapshot
        if snapshot["hint"]:
            raw = {"text": "先在引用资料中定位相关概念，列出题目的已知条件，再逐步检查自己的推理。这里提供学习提示，不直接给出活动测验答案。",
                   "citation_ids": [snapshot["sources"][0]["chunk_id"]]}
        else:
            if on_delta is not None and hasattr(self.generator, "stream"):
                from time import monotonic
                pending=[];pending_chars=0;last_flush=None
                def flush():
                    nonlocal pending_chars,last_flush
                    if pending:
                        on_delta(''.join(pending));pending.clear();pending_chars=0;last_flush=monotonic()
                def coalesced(delta):
                    nonlocal pending_chars
                    if not isinstance(delta,str) or not delta:return
                    pending.append(delta);pending_chars+=len(delta)
                    # Show the first text immediately, then batch tokens while
                    # retaining a bounded delay on providers with slower output.
                    if last_flush is None or pending_chars>=128 or monotonic()-last_flush>=0.08:flush()
                raw = self.generator.stream(copy.deepcopy(snapshot), on_text=coalesced)
                flush()
            else:
                raw = self.generator.generate(copy.deepcopy(snapshot))
        text, citations = validate_answer(raw, snapshot["sources"])
        return text, citations, snapshot

    def _publish_delta(self,identifier,run_id,delta,*,job=None):
        if not isinstance(delta,str) or not delta:return
        initial=self.repository.get_record('messages',identifier,lock=False)
        for attempt in range(3):
            snapshot=self.repository.get_record('messages',identifier,lock=False)['snapshot']
            plan=self.source_access.prepare_delivery(delivery_source_refs(snapshot),initial['space_id'],
                assistance_scope=self._assistance_scope(snapshot))
            def deliver():
                with self.repository.transaction():
                    assisted,space=self.source_access.lock_delivery(plan,initial['space_id'])
                    current=self.repository.get_record('messages',identifier)
                    if job:job.check()
                    if current['run_id']!=run_id or current['status']!='generating' or self._terminal(current):
                        raise MessageGenerationError('STALE_LEARNING_CONTEXT')
                    if current['snapshot']!=snapshot:raise AssistanceDeliveryChanged()
                    error=self._context_error(space,current,snapshot)
                    if error:raise MessageGenerationError(error)
                    self.source_access.record_delivery(assisted,plan,kind='message',
                        delivery_id=identifier,space_id=initial['space_id'])
                    current['snapshot']['delivered_source_refs']=plan['sources']
                    self.repository.put_record('messages',current)
                    self.runs.append_event(run_id,'message.delta',{'delta':delta,'provisional':True})
            try:
                return self.commands._execute('message.delta',initial['space_id'],{},None,deliver)
            except AssistanceDeliveryChanged:
                if attempt==2:raise MessageGenerationError('STALE_LEARNING_CONTEXT') from None

    def _publish_answer(self, initial, identifier, snapshot, text, citations, error, answer_status, *, error_details=None, job=None):
        for attempt in range(3):
            plan = self.source_access.prepare_delivery(delivery_source_refs(snapshot), initial["space_id"], assistance_scope=self._assistance_scope(snapshot)) if error is None else None
            try:
                return self.commands._execute("message.publish", initial["space_id"], {}, None,
                    lambda: self._publish_answer_once(initial, identifier, snapshot, text, citations,
                        error, answer_status, error_details=error_details, job=job, delivery_plan=plan))
            except AssistanceDeliveryChanged:
                if attempt == 2:
                    # Continuous assessment creation must never release unaudited
                    # text. Fail closed without calling the provider again.
                    error, error_details = "STALE_LEARNING_CONTEXT", {}
            except DomainConflict as exc:
                if exc.code != "BOUND_VERSION_UNAVAILABLE":
                    raise
                error, error_details = "STALE_LEARNING_CONTEXT", {}
                break
        return self._publish_answer_once(initial, identifier, snapshot, None, None, error,
                                         answer_status, error_details=error_details, job=job)

    def _publish_answer_once(self, initial, identifier, snapshot, text, citations, error, answer_status, *, error_details=None, job=None, delivery_plan=None):
        try:
            with self.repository.transaction():
                if delivery_plan is not None:
                    assisted, space = self.source_access.lock_delivery(delivery_plan, initial["space_id"])
                else:
                    space = self.spaces.repository.get(initial["space_id"])
                current = self.repository.get_record("messages", identifier)
                if job is not None:
                    job.check()
                if current["status"] != "generating" or self._terminal(current):
                    return
                context_error = self._context_error(space, current, snapshot)
                if context_error:
                    error, error_details = context_error, {}
                if error:
                    if snapshot.get('rag_failure_journal'):
                        current['snapshot']['rag_failure_journal'] = snapshot['rag_failure_journal']
                    public = {"UNSUPPORTED_MODEL": "当前模型不支持所需能力", "RATE_LIMITED": "模型请求过于频繁，请稍后重试",
                              "MODEL_UNAVAILABLE": "对话模型暂时不可用", "MESSAGE_VALIDATION_FAILED": "回答未通过来源校验",
                              "STALE_LEARNING_CONTEXT": "学习范围已变化，请重新发送消息", "SESSION_FINISHED": "学习会话已经结束",
                              "EMBEDDING_UNAVAILABLE": "向量模型暂时不可用", "EMBEDDING_INVALID_RESPONSE": "向量模型返回无效数据",
                              "EMBEDDING_INPUT_INVALID": "检索文本不符合向量模型要求",
                              "VECTOR_UNAVAILABLE": "向量检索服务暂时不可用", "VECTOR_INDEX_NOT_READY": "当前学习资料的向量索引尚未就绪",
                              "VECTOR_PROFILE_MISMATCH": "向量索引配置不匹配，需要重建索引",
                              "RETRIEVAL_VALIDATION_FAILED": "检索结果未通过来源校验"}
                    public["MESSAGE_ROUTING_FAILED"] = "暂未判断本次回答依据，请重试。"
                    public["CONTEXT_BUDGET_EXCEEDED"] = "当前问题与引用资料超过上下文预算，请缩短问题或调整预算"
                    public.update({
                        "RAG_INDEX_NOT_READY": "当前学习范围的内容索引尚未发布",
                        "RAG_DEADLINE_EXCEEDED": "本次检索与回答超过时间预算",
                        "RAG_STAGE_BUDGET_EXCEEDED": "剩余预算不足以完成回答及核验",
                        "MODEL_OUTPUT_CAPACITY_EXCEEDED": "本次回答超过结构化输出容量",
                        "ANSWER_VERIFICATION_FAILED": "回答修正后仍未通过核验，本次回答未发布",
                        "RAG_COST_BUDGET_EXCEEDED": "本次检索与回答超过费用预算",
                        "RAG_SPEND_CONFIG_INVALID": "检索与回答的费用预算配置无效",
                        "RAG_EXECUTION_INTERRUPTED": "上次回答执行已中断，为避免重复调用，本次任务未重放，请重新发送消息",
                        "RETRIEVAL_DEADLINE_EXCEEDED": "本次检索超过时间预算",
                        "VERIFICATION_UNAVAILABLE": "回答核验服务未能完成，本次回答未发布",
                        "GENERATION_INVALID_RESPONSE": "回答格式未通过校验",
                        "MODEL_INVALID_RESPONSE": "模型返回格式无效",
                        "MODEL_TOKEN_BUDGET_EXCEEDED": "问题与证据超过模型输入预算",
                        "RERANK_UNAVAILABLE": "证据重排服务暂时不可用",
                        "RERANK_INVALID_RESPONSE": "证据重排结果无效",
                        "RERANK_TOKEN_BUDGET_EXCEEDED": "检索候选超过重排输入预算",
                        "RERANK_CANDIDATE_BUDGET_EXCEEDED": "检索候选超过重排数量预算",
                        "RAG_SOURCE_INVALID": "原文映射未通过校验",
                        "RAG_CANDIDATES_INVALID": "检索候选未通过身份校验",
                        "RAG_RERANK_INVALID": "重排结果未通过身份校验",
                        "RETRIEVAL_SCOPE_INVALID": "检索范围无效",
                        "RETRIEVAL_INVALID_RESPONSE": "检索结果无效",
                    })
                    if error not in public:
                        error = "MODEL_UNAVAILABLE"
                        error_details = {}
                    failure = {"code": error, "message": public.get(error, "对话生成失败"),
                               "details": safe_error_details(error_details), "retryable": error in TRANSIENT_ERRORS}
                    self.runs.withdraw_drafts(current['run_id'])
                    if job is not None and not snapshot.get('rag_mode') and job.retry(failure):
                        current["status"] = "pending"
                        self.repository.put_record("messages", current)
                        return
                    self.runs.fail(current["run_id"], failure)
                    current["status"] = "failed"
                else:
                    self.source_access.record_delivery(assisted, delivery_plan,
                        kind="message", delivery_id=identifier, space_id=current["space_id"])
                    current["snapshot"]["delivered_source_refs"] = delivery_plan["sources"]
                    response = {"message_id": identifier, "session_id": current["conversation_id"], "text": text, "citations": citations}
                    if snapshot.get('request_scope'):
                        response['request_scope'] = copy.deepcopy(snapshot['request_scope'])
                    if "requested_answer_mode" in snapshot:
                        response["answer_mode"] = snapshot["answer_mode"]
                        response["supplement_explanation"] = snapshot.get("supplement_explanation")
                    if answer_status is not None:
                        response["answer_status"] = answer_status
                    existing_deltas = [event for event in self.runs.events_for(current["run_id"])
                                       if event["event"] == "message.delta"]
                    if not existing_deltas:
                        for offset in range(0, len(text), 256):
                            self.runs.append_event(current["run_id"], "message.delta", {"delta": text[offset:offset + 256]})
                    self.runs.append_event(current["run_id"], "message.completed", response)
                    self.runs.complete(current["run_id"], {"type": "message", "id": identifier, "space_id": current["space_id"]})
                    current.update(status="completed", response=response)
                    from knowpath_backend.learning.spaces.profile_candidates import infer_candidates
                    current["snapshot"]["profile_candidates"] = infer_candidates(current)
                self.repository.put_record("messages", current)
                if job is not None:
                    job.settle()
        except DomainNotFound:
            return  # A deleted space must never be recreated by a late result.

    def _context_error(self, space, current, snapshot):
        if space["scope_version"] != snapshot["scope_version"] or space["bindings"] != snapshot["bindings"]:
            return "STALE_LEARNING_CONTEXT"
        reading = current['snapshot'].get('reading_context')
        if reading:
            if snapshot.get('reading_context') != reading or snapshot.get('lesson_id'):
                return 'RETRIEVAL_SCOPE_INVALID'
            try:
                resolve_reading_scope(space, reading)
                allowed = {(s['material_id'], s['material_version_id'], s['chunk_id'])
                           for s in self._reading_sources(space, reading)}
            except DomainConflict:
                return 'STALE_LEARNING_CONTEXT'
            if any((s['material_id'], s['material_version_id'], s['chunk_id']) not in allowed
                   for s in delivery_source_refs(snapshot)):
                return 'RETRIEVAL_SCOPE_INVALID'
        if current['snapshot'].get('lesson_id'):
            if space['status']=='archived':return 'STALE_LEARNING_CONTEXT'
            lesson=self.repository.get_record('study_lessons',current['snapshot']['lesson_id'])
            if lesson['status']!='completed' or self.lessons._stale(lesson) or lesson['context_fingerprint']!=current['snapshot']['lesson_context_fingerprint']:
                return 'STALE_LEARNING_CONTEXT'
            allowed={tuple(k) for k in current['snapshot']['lesson_source_keys']}
            if any((s['material_id'],s['material_version_id'],s['chunk_id']) not in allowed for s in snapshot['sources']):return 'RETRIEVAL_SCOPE_INVALID'
        request_scope = current['snapshot'].get('request_scope')
        if request_scope is not None:
            if snapshot.get('request_scope') != request_scope:
                return 'RETRIEVAL_SCOPE_INVALID'
            if filter_document_sources(snapshot['sources'], request_scope) != snapshot['sources']:
                return 'RETRIEVAL_SCOPE_INVALID'
            if filter_document_sources(delivery_source_refs(snapshot),request_scope)!=delivery_source_refs(snapshot):return 'RETRIEVAL_SCOPE_INVALID'
        conversation = self.repository.get_record("conversations", current["conversation_id"])
        if conversation.get("learning_session_id"):
            learning = self.repository.get_record("sessions", conversation["learning_session_id"])
            if learning["status"] != "active":
                return "SESSION_FINISHED"
        return None

    def history(self,space_id,session_id,*,limit=20,cursor=None):
        import base64, binascii, json
        from knowpath_backend.learning.notes.service import fingerprint
        conversation=self.repository.get_record('conversations',session_id,lock=False)
        if conversation['space_id']!=space_id:raise DomainNotFound('conversation',session_id)
        if type(limit) is not int or not 1<=limit<=100:raise DomainConflict('INVALID_LIMIT','limit 必须为 1—100')
        scope=fingerprint(['conversation.history',space_id,session_id]);before=None
        if cursor is not None:
            try:
                if not isinstance(cursor,str) or len(cursor)>2048:raise ValueError()
                token=json.loads(base64.b64decode(cursor.encode('ascii'),altchars=b'-_',validate=True))
                if not isinstance(token,list) or len(token)!=3 or token[:2]!=[1,scope] or type(token[2]) is not int or token[2]<1:raise ValueError()
                before=token[2]
            except (ValueError,TypeError,UnicodeError,binascii.Error):raise DomainConflict('INVALID_CURSOR','分页游标无效') from None
        for _ in range(3):
            rows=self.repository.records('messages',conversation_id=session_id,lock=False)
            remaining=sorted([r for r in rows if before is None or r['sequence']<before],key=lambda r:r['sequence'],reverse=True)
            items=list(reversed(remaining[:limit]))
            next_cursor=base64.urlsafe_b64encode(json.dumps([1,scope,items[0]['sequence']]).encode()).decode() if len(remaining)>limit else None
            page={'items':items,'next_cursor':next_cursor}
            refs=[r for row in page['items'] if row['status']=='completed' for r in message_source_refs(row,rows)]
            if any(row['response'] and not message_source_refs(row,rows) and 'delivered_source_refs' not in row['snapshot'] for row in page['items']):
                raise DomainConflict('MESSAGE_CONTEXT_UNAVAILABLE','历史回答来源不可用')
            plan=self.source_access.prepare_delivery(refs,space_id, assistance_scope=self._page_assistance(page["items"]))
            try:
                with self.repository.transaction():
                    active,_=self.source_access.lock_delivery(plan,space_id)
                    fresh=[self.repository.get_record('messages',r['id']) for r in page['items']]
                    if fingerprint(fresh)!=fingerprint(page['items']):raise AssistanceDeliveryChanged()
                    if any(r['response'] for r in fresh):self.source_access.record_delivery(active,plan,kind='conversation_read',delivery_id=uid(),space_id=space_id)
                    return {'items':[{**{k:copy.deepcopy(r.get(k)) for k in ('id','sequence','message','response','status','run_id','created_at')},
                                      'reading_context': copy.deepcopy(r['snapshot'].get('reading_context')),
                                      'lesson_id': r['snapshot'].get('lesson_id')} for r in fresh],'next_cursor':page['next_cursor']}
            except AssistanceDeliveryChanged:continue
        raise DomainConflict('STALE_LEARNING_CONTEXT','会话在读取期间更新')

    def _record_sources(self, identifier, snapshot, *, job=None, publish_sources=True):
        try:
            initial = self.repository.get_record("messages", identifier, lock=False)
            with self.repository.transaction():
                space = self.spaces.repository.get(initial["space_id"])
                current = self.repository.get_record("messages", identifier)
                if job is not None:
                    job.check()
                if current["status"] != "generating" or self._terminal(current):
                    return False
                error = self._context_error(space, current, snapshot)
                if error:
                    raise RetrievalError(error)
                if "delivered_source_refs" in current["snapshot"]:
                    snapshot = {**snapshot, "delivered_source_refs": current["snapshot"]["delivered_source_refs"]}
                current["snapshot"] = snapshot
                self.repository.put_record("messages", current)
                route_events = [e for e in self.runs.events_for(current["run_id"]) if e["event"] == "message.route"]
                if snapshot.get("semantic_route") and (not route_events or route_events[-1]["data"].get("answer_mode") != snapshot["answer_mode"] or route_events[-1]["data"].get("supplement_explanation") != snapshot.get("supplement_explanation")):
                    self.runs.append_event(current["run_id"], "message.route", {"answer_mode": snapshot["answer_mode"], "supplement_explanation": snapshot.get("supplement_explanation")})
                if not publish_sources:
                    return True
                refs = [{k: v for k, v in row.items() if k in {
                            "material_id", "material_version_id", "chunk_id", "graph_version",
                            "retrieval_version_id", "source_spans", "citation_schema_version"}}
                        for row in snapshot["sources"]]
                self.runs.append_event(current["run_id"], "tool.completed", {"tool": "learning_sources", "source_refs": refs})
                return True
        except DomainNotFound:
            return False

    def _terminal(self, current):
        run = self.runs.get(current["run_id"])
        if run["status"] == "cancelling":
            run = self.runs.acknowledge_cancel(run["id"])
        if run["status"] not in ACTIVE_STATUSES:
            current["status"] = run["status"]
            self.repository.put_record("messages", current)
            return True
        return False
