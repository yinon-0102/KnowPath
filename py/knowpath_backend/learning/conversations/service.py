"""Durable source-bound messages; model I/O occurs outside database locks."""
import copy
import re
from uuid import uuid4

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.conversations.schemas import SendMessage
from knowpath_backend.learning.conversations.generation import DashScopeAnswerGenerator, MessageGenerationError, validate_answer
from knowpath_backend.learning.workers.runs import ACTIVE_STATUSES
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker, TRANSIENT_ERRORS, LeaseLost, enqueue
from knowpath_backend.learning.spaces.service import SpaceService, now
from knowpath_backend.learning.rag.retrieval import KeywordRetriever, RetrievalError, configured_retriever
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.conversations.context import bound_snapshot, project_memory
from knowpath_backend.learning.rag.verification import VerificationError, safe_error_details


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

    def send(self, space_id, payload, key=None, *, dispatch=None, durable=False):
        payload = SendMessage.model_validate(payload).model_dump()
        created = []
        def prepare():
            # Hint requests must serialize with grading before taking space locks.
            assessments = self.repository.records("assessments", space_id=space_id)
            space = self.spaces.repository.get(space_id)
            sources = self._sources(space)
            if not sources:
                raise DomainConflict("NO_LEARNING_SOURCES", "当前学习范围没有可引用的资料")
            conversation = self._conversation(space_id, payload["session_id"])
            previous = sorted(self.repository.records("messages", conversation_id=conversation["id"]), key=lambda m: m["sequence"])
            for message in previous:
                if message["status"] in {"pending", "generating"} and self.runs.get(message["run_id"])["status"] in ACTIVE_STATUSES:
                    raise DomainConflict("MESSAGE_IN_PROGRESS", "该会话已有生成中的消息")
            context = project_memory(self.repository.records("messages", space_id=space_id),
                                     space, conversation["id"], payload["message"])
            hint = self._hint(assessments, payload["message"])
            run = self.runs.create("message", status="queued" if durable else "running")
            identifier = uid()
            snapshot = {"message": payload["message"], "sources": sources, **context,
                        "scope_version": space["scope_version"], "bindings": copy.deepcopy(space["bindings"]), "hint": hint}
            record = {"id": identifier, "space_id": space_id, "conversation_id": conversation["id"],
                      "run_id": run["id"], "status": "pending", "message": payload["message"],
                      "sequence": len(previous) + 1, "snapshot": snapshot, "response": None, "created_at": now()}
            self.repository.put_record("messages", record)
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
            if conversation.get("learning_session_id"):
                learning = self.repository.get_record("sessions", conversation["learning_session_id"])
                if learning["status"] != "active":
                    raise DomainConflict("SESSION_FINISHED", "学习会话已经结束")
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
        touched = {}
        for assessment, question in matched:
            question["assisted"] = True
            touched[assessment["id"]] = assessment
        for assessment in touched.values():
            self.repository.put_record("assessments", assessment)
        return bool(matched)

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
                if interrupted or (self.rag_pipeline is not None and not current["snapshot"]["hint"]):
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
            if self.rag_pipeline is not None and not snapshot["hint"]:
                def cancelled():
                    if job is not None:
                        job.check()
                    return self.runs.get(current["run_id"])["status"] not in {"queued", "running"}
                result = self.rag_pipeline.answer(snapshot["message"], space_id=current["space_id"],
                    expected_scope_version=snapshot["scope_version"], expected_bindings=snapshot["bindings"],
                    cancelled=cancelled, history=copy.deepcopy(snapshot.get('history', [])))
                snapshot = {**snapshot, "sources": result["sources"], "rag_trace": result["trace"]}
                if not self._record_sources(identifier, snapshot, job=job):
                    return
                text, citations, answer_status = result["text"], result["citations"], result["status"]
            else:
                text, citations, snapshot = self._legacy_answer(identifier, snapshot, job=job)
                if text is None:
                    return
        except LeaseLost:
            raise
        except VerificationError as exc:
            error = exc.code
            error_details = safe_error_details(exc.details)
            from knowpath_backend.learning.rag.diagnostics import safe_journal_snapshot
            snapshot['rag_failure_journal'] = safe_journal_snapshot(getattr(exc, 'call_journal', None))
        except (MessageGenerationError, RetrievalError) as exc:
            error = exc.code
        except Exception:
            # Custom providers must not leak request/credential details.
            error = "MODEL_UNAVAILABLE"
        self._publish_answer(initial, identifier, snapshot, text if error is None else None,
                             citations if error is None else None, error, answer_status,
                             error_details=error_details, job=job)

    def _legacy_answer(self, identifier, snapshot, *, job=None):
        # Active assessment hints remain deterministic and require no provider.
        retriever = KeywordRetriever() if snapshot["hint"] else self.retriever
        retrieval_sources = snapshot.get("retrieval_sources", snapshot["sources"])
        try:
            selected = retriever.select(snapshot["message"], copy.deepcopy(retrieval_sources), limit=8)
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
            raw = self.generator.generate(copy.deepcopy(snapshot))
        text, citations = validate_answer(raw, snapshot["sources"])
        return text, citations, snapshot

    def _publish_answer(self, initial, identifier, snapshot, text, citations, error, answer_status, *, error_details=None, job=None):
        try:
            with self.repository.transaction():
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
                    if job is not None and not snapshot.get('rag_mode') and job.retry(failure):
                        current["status"] = "pending"
                        self.repository.put_record("messages", current)
                        return
                    self.runs.fail(current["run_id"], failure)
                    current["status"] = "failed"
                else:
                    response = {"message_id": identifier, "session_id": current["conversation_id"], "text": text, "citations": citations}
                    if answer_status is not None:
                        response["answer_status"] = answer_status
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
        conversation = self.repository.get_record("conversations", current["conversation_id"])
        if conversation.get("learning_session_id"):
            learning = self.repository.get_record("sessions", conversation["learning_session_id"])
            if learning["status"] != "active":
                return "SESSION_FINISHED"
        return None

    def _record_sources(self, identifier, snapshot, *, job=None):
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
                current["snapshot"] = snapshot
                self.repository.put_record("messages", current)
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
