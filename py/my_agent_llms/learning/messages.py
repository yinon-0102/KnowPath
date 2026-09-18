"""Durable source-bound messages; model I/O occurs outside database locks."""
import copy
import re
from uuid import uuid4

from .errors import DomainConflict, DomainNotFound
from .message_schemas import SendMessage
from .message_generation import DashScopeAnswerGenerator, MessageGenerationError, validate_answer
from .runs import ACTIVE_STATUSES
from .spaces import SpaceService, now


def uid():
    return str(uuid4())


class MessageService:
    def __init__(self, repository, spaces, assessments, runs, generator=None):
        self.repository, self.spaces, self.assessments, self.runs = repository, spaces, assessments, runs
        self.generator = generator if generator is not None else DashScopeAnswerGenerator()
        self.commands = SpaceService(repository, spaces.materials)

    def send(self, space_id, payload, key=None, *, dispatch=None):
        payload = SendMessage.model_validate(payload).model_dump()
        created = []
        def prepare():
            # Hint requests must serialize with grading before taking space locks.
            assessments = self.repository.records("assessments", space_id=space_id)
            space = self.spaces.repository.get(space_id)
            sources = self._sources(space, payload["message"])
            if not sources:
                raise DomainConflict("NO_LEARNING_SOURCES", "当前学习范围没有可引用的资料")
            conversation = self._conversation(space_id, payload["session_id"])
            previous = sorted(self.repository.records("messages", conversation_id=conversation["id"]), key=lambda m: m["sequence"])
            for message in previous:
                if message["status"] in {"pending", "generating"} and self.runs.get(message["run_id"])["status"] in ACTIVE_STATUSES:
                    raise DomainConflict("MESSAGE_IN_PROGRESS", "该会话已有生成中的消息")
            history = []
            for message in previous:
                if (message["status"] == "completed" and message["snapshot"]["scope_version"] == space["scope_version"]
                        and message["snapshot"]["bindings"] == space["bindings"]):
                    history.extend([{"role": "user", "content": message["message"]},
                                    {"role": "assistant", "content": message["response"]["text"]}])
            hint = self._hint(assessments, payload["message"])
            run = self.runs.create("message", status="running")
            identifier = uid()
            snapshot = {"message": payload["message"], "sources": sources, "history": history[-10:],
                        "scope_version": space["scope_version"], "bindings": copy.deepcopy(space["bindings"]), "hint": hint}
            record = {"id": identifier, "space_id": space_id, "conversation_id": conversation["id"],
                      "run_id": run["id"], "status": "pending", "message": payload["message"],
                      "sequence": len(previous) + 1, "snapshot": snapshot, "response": None, "created_at": now()}
            self.repository.put_record("messages", record)
            refs = [{k: v for k, v in row.items() if k not in {"text", "topic_id", "topic_name"}} for row in sources]
            self.runs.append_event(run["id"], "tool.completed", {"tool": "learning_sources", "source_refs": refs})
            created.append((identifier, run["id"]))
            return {"run_id": run["id"], "session_id": conversation["id"], "status": "running"}
        response = self.commands._execute("message.send", space_id, payload, key, prepare)
        # A losing same-key transaction can leave a local preparation record.
        # Only dispatch the preparation whose Run was actually committed.
        if created and created[-1][1] == response["run_id"]:
            if dispatch is None:
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

    def _sources(self, space, message):
        tokens = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", message.casefold()))
        sources, versions = {}, {}
        for topic in self.assessments._topics(space):
            for ref in topic["source_refs"]:
                version_id = ref["material_version_id"]
                if version_id not in versions:
                    versions[version_id] = self.spaces.materials.get_version(version_id)
                version = versions[version_id]
                if version is None or version.material_id != ref["material_id"]:
                    raise DomainConflict("BOUND_VERSION_UNAVAILABLE", "绑定资料版本不可用")
                chunk = next((c for c in version.chunks if c.id == ref["chunk_id"]), None)
                if chunk is not None:
                    sources[chunk.id] = {**ref, "topic_id": topic["id"], "topic_name": topic["name"], "text": chunk.text[:6000]}
        def rank(row):
            searchable = (row["topic_name"] + " " + row["text"]).casefold()
            return (-sum(token in searchable for token in tokens), row["chunk_id"])
        return sorted(sources.values(), key=rank)[:8]

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

    def generate(self, identifier):
        try:
            initial = self.repository.get_record("messages", identifier, lock=False)
            with self.repository.transaction():
                self.spaces.repository.get(initial["space_id"])
                current = self.repository.get_record("messages", identifier)
                if current["status"] != "pending":
                    return
                if self._terminal(current):
                    return
                current["status"] = "generating"
                self.repository.put_record("messages", current)
        except DomainNotFound:
            return
        snapshot = current["snapshot"]
        error = None
        try:
            if snapshot["hint"]:
                raw = {"text": "先在引用资料中定位相关概念，列出题目的已知条件，再逐步检查自己的推理。这里提供学习提示，不直接给出活动测验答案。",
                       "citation_ids": [snapshot["sources"][0]["chunk_id"]]}
            else:
                raw = self.generator.generate(snapshot)
            text, citations = validate_answer(raw, snapshot["sources"])
        except MessageGenerationError as exc:
            error = exc.code
        except Exception:
            # An injected/custom provider must never leak upstream credentials or
            # exception details through Run/SSE, or leave the run active forever.
            error = "MODEL_UNAVAILABLE"
        try:
            with self.repository.transaction():
                space = self.spaces.repository.get(initial["space_id"])
                current = self.repository.get_record("messages", identifier)
                if current["status"] != "generating" or self._terminal(current):
                    return
                if space["scope_version"] != snapshot["scope_version"] or space["bindings"] != snapshot["bindings"]:
                    error = "STALE_LEARNING_CONTEXT"
                conversation = self.repository.get_record("conversations", current["conversation_id"])
                if conversation.get("learning_session_id"):
                    learning = self.repository.get_record("sessions", conversation["learning_session_id"])
                    if learning["status"] != "active":
                        error = "SESSION_FINISHED"
                if error:
                    public = {"MODEL_UNAVAILABLE": "对话模型暂时不可用", "MESSAGE_VALIDATION_FAILED": "回答未通过来源校验",
                              "STALE_LEARNING_CONTEXT": "学习范围已变化，请重新发送消息", "SESSION_FINISHED": "学习会话已经结束"}
                    self.runs.fail(current["run_id"], {"code": error, "message": public.get(error, "对话生成失败"),
                        "details": {}, "retryable": error == "MODEL_UNAVAILABLE"})
                    current["status"] = "failed"
                else:
                    response = {"message_id": identifier, "session_id": current["conversation_id"], "text": text, "citations": citations}
                    for offset in range(0, len(text), 256):
                        self.runs.append_event(current["run_id"], "message.delta", {"delta": text[offset:offset + 256]})
                    self.runs.append_event(current["run_id"], "message.completed", response)
                    self.runs.complete(current["run_id"], {"type": "message", "id": identifier, "space_id": current["space_id"]})
                    current.update(status="completed", response=response)
                self.repository.put_record("messages", current)
        except DomainNotFound:
            return  # A deleted space must never be recreated by a late result.

    def _terminal(self, current):
        run = self.runs.get(current["run_id"])
        if run["status"] == "cancelling":
            run = self.runs.acknowledge_cancel(run["id"])
        if run["status"] not in ACTIVE_STATUSES:
            current["status"] = run["status"]
            self.repository.put_record("messages", current)
            return True
        return False
