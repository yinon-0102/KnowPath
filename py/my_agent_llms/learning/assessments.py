"""Assessment commands: immutable answers/evidence, atomic grading and replay."""
from __future__ import annotations

import copy
import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4

from .assessment_schemas import CreateAssessment, RecordAttempt, FinalizeAssessment, ResetState
from .errors import DomainConflict, DomainNotFound
from .mastery import aggregate, MasteryPolicy
from .question_generation import DashScopeQuestionGenerator, QuestionGenerationError, validate_questions
from .spaces import SpaceService, now


def uid():
    return str(uuid4())


def revision(topic):
    return sha256(json.dumps(topic["source_refs"], sort_keys=True).encode()).hexdigest()


class AssessmentService:
    def __init__(self, repository, spaces, runs, generator=None, *, mastery_policy=None):
        self.repository, self.spaces, self.runs = repository, spaces, runs
        self.generator = generator if generator is not None else DashScopeQuestionGenerator()
        self.commands = SpaceService(repository, spaces.materials)
        self.mastery_policy = mastery_policy or MasteryPolicy()

    def _execute(self, operation, identifier, payload, key, change):
        return self.commands._execute(operation, identifier, payload, key, change)

    def _topics(self, space):
        topics = self.spaces.bound_topics(space)
        selected, excluded = set(space["topic_ids"]), set(space["excluded_topic_ids"])
        return [dict(topic, revision_id=revision(topic)) for topic in topics
                if (not selected or topic["id"] in selected) and topic["id"] not in excluded]

    def _epochs(self, space_id):
        result = {}
        for reset in self.repository.records("resets", space_id=space_id):
            for topic_id in reset["topic_ids"]:
                result[topic_id] = max(result.get(topic_id, 0), reset["state_version"])
        return result

    def create(self, space_id, payload, key=None, *, dispatch=None):
        payload = CreateAssessment.model_validate(payload).model_dump(exclude_none=True)
        created = []
        def prepare():
            space = self.spaces.repository.get(space_id)
            topics = self._topics(space)
            requested = payload.get("topic_ids") or [t["id"] for t in topics]
            if not requested or not set(requested) <= {t["id"] for t in topics}:
                raise DomainConflict("TOPIC_OUT_OF_SCOPE", "测验主题必须属于当前学习范围")
            topics = [t for t in topics if t["id"] in requested]
            # Freeze actual source text and refs before leaving the transaction.
            for topic in topics:
                texts = []
                for ref in topic["source_refs"]:
                    version = self.spaces.materials.get_version(ref["material_version_id"])
                    texts.extend(c.text for c in version.chunks if c.id == ref["chunk_id"])
                topic["source_text"] = "\n\n".join(texts)
            run = self.runs.create("assessment_generation", status="running")
            assessment = {"id": uid(), "space_id": space_id, "kind": payload["kind"],
                "status": "generating", "topic_ids": requested, "questions": [], "result": None,
                "run_id": run["id"], "finalize_run_id": None, "submission_id": None,
                "created_at": now(), "snapshot": {"scope_version": space["scope_version"],
                    "bindings": copy.deepcopy(space["bindings"]), "topics": topics,
                    "epochs": self._epochs(space_id), "request": payload,
                    "assessment_policy_version": "assessment-v1"}}
            self.repository.put_record("assessments", assessment)
            created.append(assessment["id"])
            return self.public(assessment)
        response = self._execute("assessment.create", space_id, payload, key, prepare)
        if created and response["id"] in created:
            if dispatch is None:
                self.generate(response["id"])
            else:
                dispatch(self.generate, response["id"])
        return response

    def generate(self, assessment_id):
        assessment = self.repository.get_record("assessments", assessment_id)
        if assessment["status"] != "generating":
            return
        snapshot = assessment["snapshot"]
        error = None
        try:
            raw = self.generator.generate(snapshot["topics"], snapshot["request"])
            questions = validate_questions(raw, snapshot["topics"], snapshot["request"])
        except QuestionGenerationError as exc:
            error = {"code": exc.code, "message": str(exc), "details": {}, "retryable": exc.code == "MODEL_UNAVAILABLE"}
        with self.repository.transaction():
            current = self.repository.get_record("assessments", assessment_id)
            if current["status"] != "generating":
                return
            run = self.runs.get(current["run_id"])
            if run["status"] in {"cancelling", "cancelled"}:
                self.runs.acknowledge_cancel(run["id"])
                current["status"] = "cancelled"
            elif run["status"] != "running":
                current["status"] = "failed"
            elif error:
                self.runs.fail(run["id"], error)
                current["status"] = "failed"
            else:
                current.update(status="ready", questions=questions)
                self.runs.complete(run["id"], {"type": "assessment", "id": assessment_id})
            self.repository.put_record("assessments", current)

    def public(self, assessment):
        snapshot = assessment["snapshot"]
        result = {k: copy.deepcopy(assessment[k]) for k in ("id", "space_id", "kind", "status", "topic_ids", "created_at", "run_id")}
        result.update(question_count=snapshot["request"]["question_count"], scope_version=snapshot["scope_version"],
                      graph_versions=[b["graph_version"] for b in snapshot["bindings"]],
                      material_version_ids=[b["material_version_id"] for b in snapshot["bindings"]],
                      assessment_policy_version=snapshot["assessment_policy_version"], questions=[], answers=[])
        if assessment["status"] not in {"generating", "failed", "cancelled"}:
            result["questions"] = [{k: copy.deepcopy(q[k]) for k in ("id", "type", "prompt", "options", "difficulty") if k in q}
                                   | {"topic_ids": [q["topic_id"]]} for q in assessment["questions"]]
            answers = self._latest(assessment["id"])
            result["answers"] = [{"question_id": qid, "answer_revision": a["answer_revision"], "answer": a["answer"]}
                                 for qid, a in answers.items()]
        if assessment["status"] == "completed":
            result["result_url"] = f"/api/v1/assessments/{assessment['id']}/result"
        return result

    def get(self, assessment_id):
        assessment = self.repository.get_record("assessments", assessment_id)
        if assessment["status"] == "generating":
            run = self.runs.get(assessment["run_id"])
            if run["status"] in {"failed", "cancelled"}:
                assessment["status"] = run["status"]
        return self.public(assessment)

    def _latest(self, assessment_id):
        answers = {}
        for answer in self.repository.records("attempts", assessment_id=assessment_id):
            old = answers.get(answer["question_id"])
            if old is None or answer["answer_revision"] > old["answer_revision"]:
                answers[answer["question_id"]] = answer
        return answers

    def record(self, assessment_id, payload, key=None):
        payload = RecordAttempt.model_validate(payload).model_dump()
        def change():
            assessment = self.repository.get_record("assessments", assessment_id)
            if assessment["status"] not in {"ready", "in_progress"}:
                raise DomainConflict("ASSESSMENT_FINALIZED", "测验不再接受答题")
            questions = {q["id"]: q for q in assessment["questions"]}
            latest = self._latest(assessment_id)
            timestamp, batch_id = now(), uid()
            for item in payload["answers"]:
                question = questions.get(item["question_id"])
                if question is None:
                    raise DomainConflict("QUESTION_NOT_IN_ASSESSMENT", "题目不属于当前测验")
                old = latest.get(item["question_id"])
                current_revision = old["answer_revision"] if old else 0
                if current_revision != item["expected_answer_revision"]:
                    raise DomainConflict("VERSION_CONFLICT", "答案版本已变化")
                if question["type"] == "single_choice" and item["answer"] not in {o["id"] for o in question["options"]}:
                    raise DomainConflict("INVALID_ANSWER", "单选答案必须是有效选项 ID")
                record = {"id": uid(), "assessment_id": assessment_id, "question_id": item["question_id"],
                          "answer": item["answer"], "answer_revision": current_revision + 1,
                          "assisted": bool(question.get("assisted", False) or (old and old["assisted"])),
                          "elapsed_seconds": item["elapsed_seconds"], "created_at": timestamp}
                self.repository.put_record("attempts", record)
                latest[item["question_id"]] = record
            assessment["status"] = "in_progress"
            self.repository.put_record("assessments", assessment)
            if payload["finalize"]:
                return self._finalize(assessment, False)
            return {"attempt_id": batch_id, "status": "recorded", "accepted_count": len(payload["answers"]),
                    "next_question_id": next((qid for qid in questions if qid not in latest), None)}
        return self._execute("assessment.attempt", assessment_id, payload, key, change)

    def finalize(self, assessment_id, payload):
        payload = FinalizeAssessment.model_validate(payload).model_dump()
        def change():
            return self._finalize(self.repository.get_record("assessments", assessment_id), payload["allow_unanswered"])
        return self._execute("assessment.finalize", assessment_id, payload, None, change)

    def _finalize(self, assessment, allow_unanswered):
        if assessment["status"] == "completed":
            return {"run_id": assessment["finalize_run_id"], "assessment_id": assessment["id"], "status": "processing"}
        if assessment["status"] not in {"ready", "in_progress"}:
            raise DomainConflict("ASSESSMENT_NOT_READY", "测验尚未就绪")
        answers = self._latest(assessment["id"])
        if not allow_unanswered and len(answers) != len(assessment["questions"]):
            raise DomainConflict("ASSESSMENT_INCOMPLETE", "仍有未回答题目")
        space = self.spaces.repository.get(assessment["space_id"])
        # Bindings, rather than mutable scope, determine whether old scores apply.
        current_topics = {t["id"]: revision(t) for t in self.spaces.bound_topics(space)}
        epochs = self._epochs(space["id"])
        run = self.runs.create("assessment_finalize", status="running")
        assessment.update(submission_id=uid(), finalize_run_id=run["id"])
        timestamp, question_results, topic_results, evidence = now(), [], [], []
        for question in assessment["questions"]:
            answer = answers.get(question["id"])
            score, verdict, feedback = None, "unverified", "insufficient_evidence: 未作答"
            if answer is not None:
                if question["type"] == "single_choice":
                    score = 1.0 if answer["answer"] == question["answer_key"] else 0.0
                    verdict = "correct" if score else "incorrect"
                    feedback = "已按封存客观答案判分"
                else:
                    feedback = "开放题尚无可靠判分，保存反馈并建议替代客观题"
            topic_id = question["topic_id"]
            epoch = assessment["snapshot"]["epochs"].get(topic_id, 0)
            assisted = bool(question.get("assisted") or (answer and answer["assisted"]))
            eligible = (score is not None and not assisted and epoch == epochs.get(topic_id, 0)
                        and question["topic_revision_id"] == current_topics.get(topic_id))
            record = {"id": uid(), "space_id": space["id"], "topic_id": topic_id,
                "assessment_id": assessment["id"], "attempt_id": answer["id"] if answer else None,
                "question_id": question["id"], "submission_id": assessment["submission_id"],
                "topic_revision_id": question["topic_revision_id"], "family_id": question["family_id"],
                "rubric_version": question["rubric_version"], "is_application": question.get("is_application", False),
                "assisted": assisted, "eligible": eligible, "epoch": epoch,
                "submission_sequence": space["state_version"] + 1,
                "kind": "objective_answer" if question["type"] == "single_choice" else "short_answer",
                "result": verdict, "score": score, "error_tags": ["incorrect_answer"] if verdict == "incorrect" else [],
                "source_refs": question["source_refs"], "created_at": timestamp}
            self.repository.put_record("evidence", record)
            evidence.append(record)
            question_results.append({"question_id": question["id"], "verdict": verdict, "score": score,
                "feedback": feedback, "rubric_version": question["rubric_version"], "source_refs": question["source_refs"],
                "evidence_id": record["id"], "assisted": assisted})
        version = space["state_version"] + 1
        for topic_id in dict.fromkeys(q["topic_id"] for q in assessment["questions"]):
            rows = [e for e in evidence if e["topic_id"] == topic_id]
            verified = [e for e in rows if e["score"] is not None]
            topic_results.append({"topic_id": topic_id, "topic_revision_id": rows[0]["topic_revision_id"],
                "score": sum(e["score"] for e in verified) / len(verified) if verified else None,
                "verified_count": len(verified), "unverified_count": len(rows) - len(verified),
                "error_tags": sorted({tag for e in rows for tag in e["error_tags"]}),
                "evidence_ids": [e["id"] for e in rows]})
            if rows[0]["epoch"] != epochs.get(topic_id, 0) or rows[0]["topic_revision_id"] != current_topics.get(topic_id):
                continue
            self._recompute(space["id"], topic_id, current_topics[topic_id], epochs.get(topic_id, 0), version, timestamp)
        space["state_version"] = version
        self.spaces.repository.put(space)
        assessment.update(status="completed", result={"assessment_id": assessment["id"], "graded_at": timestamp,
            "topic_results": topic_results, "question_results": question_results, "state_version": version,
            "plan_replan_run_id": None})
        self.repository.put_record("assessments", assessment)
        self.runs.complete(run["id"], {"type": "assessment", "id": assessment["id"]})
        return {"run_id": run["id"], "assessment_id": assessment["id"], "status": "processing"}

    def _recompute(self, space_id, topic_id, revision_id, epoch, version, timestamp):
        old = self.repository.records("states", space_id=space_id, topic_id=topic_id)
        previous = old[0] if old else {}
        evidence = [e for e in self.repository.records("evidence", space_id=space_id, topic_id=topic_id)
                    if e.get("epoch") == epoch and e.get("topic_revision_id") == revision_id]
        same_cycle = previous.get("epoch") == epoch and previous.get("topic_revision_id") == revision_id
        value = aggregate(topic_id, revision_id, evidence, previous if same_cycle else {}, version, timestamp, policy=self.mastery_policy)
        value.update(id=previous.get("id", uid()), space_id=space_id, epoch=epoch)
        self.repository.put_record("states", value)

    def result(self, assessment_id):
        assessment = self.repository.get_record("assessments", assessment_id)
        if assessment["result"] is None:
            raise DomainConflict("ASSESSMENT_NOT_READY", "测验尚未完成评分")
        return copy.deepcopy(assessment["result"])

    def state(self, space_id, *, topic_id=None, status=None, include_evidence=False):
        with self.repository.transaction():
            space = self.spaces.repository.get(space_id)
            items = self.repository.records("states", space_id=space_id)
            current = {t["id"]: revision(t) for t in self.spaces.bound_topics(space)}
            timestamp = datetime.fromisoformat(now())
            for item in items:
                if item.get("topic_revision_id") != current.get(item["topic_id"]):
                    item.update(score_validity="stale", status="needs_review")
                elif item.get("next_review_at") and datetime.fromisoformat(item["next_review_at"]) <= timestamp:
                    item["status"] = "needs_review"
            items = [i for i in items if (topic_id is None or i["topic_id"] == topic_id) and (status is None or i["status"] == status)]
            if include_evidence:
                evidence = {e["id"]: e for e in self.repository.records("evidence", space_id=space_id)}
                for item in items:
                    item["evidence"] = [evidence[eid] for eid in item["evidence_ids"] if eid in evidence]
            return {"space_id": space_id, "state_version": space["state_version"], "items": items}

    def mark_assisted(self, space_id, question_id):
        # Locate without taking every assessment lock; then lock only its owner.
        candidates = self.repository.records("assessments", space_id=space_id)
        owner = next((a["id"] for a in candidates if any(q["id"] == question_id for q in a["questions"])), None)
        if owner is None:
            raise DomainNotFound("question", question_id)
        with self.repository.transaction():
            assessment = self.repository.get_record("assessments", owner)
            if assessment["status"] not in {"ready", "in_progress"}:
                raise DomainConflict("ASSESSMENT_FINALIZED", "测验不再接受提示请求")
            for question in assessment["questions"]:
                if question["id"] == question_id:
                    question["assisted"] = True
            self.repository.put_record("assessments", assessment)

    def evidence_page(self, space_id, *, cursor=None, limit=20, **filters):
        if not 1 <= limit <= 100:
            raise DomainConflict("INVALID_LIMIT", "limit 必须在 1 到 100 之间")
        fingerprint = sha256(json.dumps([space_id, filters], sort_keys=True, default=str).encode()).hexdigest()
        items = self.evidence(space_id, **filters)
        if cursor:
            try:
                decoded = json.loads(base64.b64decode(cursor.encode(), altchars=b"-_", validate=True))
                if not isinstance(decoded, list) or len(decoded) != 3 or decoded[0] != fingerprint or not all(isinstance(x, str) for x in decoded):
                    raise ValueError()
                items = [e for e in items if (e["created_at"], e["id"]) > tuple(decoded[1:])]
            except (ValueError, TypeError, UnicodeError):
                raise DomainConflict("INVALID_CURSOR", "分页游标无效或筛选条件已变化") from None
        page = items[:limit]
        next_cursor = None
        if len(items) > limit:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([fingerprint, last["created_at"], last["id"]]).encode()).decode()
        return {"items": page, "next_cursor": next_cursor}

    def evidence(self, space_id, *, topic_id=None, kind=None, from_time=None, to_time=None, limit=None):
        self.spaces.get(space_id)
        items = self.repository.records("evidence", space_id=space_id)
        from_time = from_time.replace(tzinfo=timezone.utc) if from_time and from_time.tzinfo is None else from_time
        to_time = to_time.replace(tzinfo=timezone.utc) if to_time and to_time.tzinfo is None else to_time
        if from_time and to_time and from_time > to_time:
            raise DomainConflict("INVALID_TIME_RANGE", "from 不能晚于 to")
        items = [e for e in items if (topic_id is None or e["topic_id"] == topic_id) and (kind is None or e["kind"] == kind)
                 and (from_time is None or datetime.fromisoformat(e["created_at"]) >= from_time)
                 and (to_time is None or datetime.fromisoformat(e["created_at"]) <= to_time)]
        return items if limit is None else items[:limit]

    def reset(self, space_id, payload, key=None):
        payload = ResetState.model_validate(payload).model_dump()
        def change():
            space = self.spaces.repository.get(space_id)
            if space["state_version"] != payload["expected_state_version"]:
                raise DomainConflict("VERSION_CONFLICT", "学习状态版本已变化")
            topics = {t["id"]: revision(t) for t in self.spaces.bound_topics(space)}
            if not set(payload["topic_ids"]) <= topics.keys():
                raise DomainConflict("TOPIC_OUT_OF_SCOPE", "主题不属于空间绑定资料")
            version, timestamp = space["state_version"] + 1, now()
            self.repository.put_record("resets", {"id": uid(), "space_id": space_id, "topic_ids": payload["topic_ids"],
                "state_version": version, "reason": payload["reason"], "created_at": timestamp})
            for topic_id in payload["topic_ids"]:
                self._recompute(space_id, topic_id, topics[topic_id], version, version, timestamp)
            space["state_version"] = version
            self.spaces.repository.put(space)
            return self.state(space_id)
        return self._execute("state.reset", space_id, payload, key, change)

    def changes(self, space_id):
        self.spaces.get(space_id)
        return [{**item, "kind": "state_reset"} for item in self.repository.records("resets", space_id=space_id)]
