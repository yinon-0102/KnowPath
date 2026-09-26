"""Source-backed, explicitly confirmed review of one frozen assessment question.

Events are append-only within the persisted assessment snapshot. A report only
quarantines; it cannot supply a score, answer revision or corrected definition.
The local authenticated user is the human adjudicator, not a second authority.
"""
from copy import deepcopy
import json
from uuid import uuid4

from knowpath_backend.learning.assessments.grade_reviews import replace_grade
from knowpath_backend.learning.assessments.grading import effective_question
from knowpath_backend.learning.assessments.schemas import QuestionReport, QuestionResolution
from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.materials.source_access import SourceAccessService, AssistanceDeliveryChanged
from knowpath_backend.learning.spaces.service import now


def _assessment(service, assessment_id, *, lock=True):
    assessment = service.repository.get_record("assessments", assessment_id, lock=lock)
    if assessment["status"] != "completed" or assessment.get("result") is None:
        raise DomainConflict("ASSESSMENT_NOT_READY", "仅已完成测验可复核封存题目")
    return assessment


def _question(assessment, question_id):
    question = next((q for q in assessment["questions"] if q["id"] == question_id), None)
    if question is None:
        raise DomainNotFound("question", question_id)
    return question


def _version(assessment):
    return len(assessment["snapshot"].get("question_review_events", []))


def _check_version(assessment, payload):
    if _version(assessment) != payload["expected_review_version"]:
        raise DomainConflict("VERSION_CONFLICT", "题目复核版本已变化，请刷新后重试")


def _source_text(assessment, question):
    return next((t.get("source_text", "") for t in assessment["snapshot"]["topics"]
                 if t["id"] == question["topic_id"]), "")


def _publish(service, assessment, question, event):
    run = service.runs.create("question_review", status="running")
    event["run_id"] = run["id"]
    event["review_version"] = _version(assessment) + 1
    assessment["snapshot"].setdefault("question_review_events", []).append(event)
    effective = effective_question(assessment, question)
    event.update(replace_grade(service, assessment, effective, event["id"], event["created_at"]))
    service.repository.put_record("assessments", assessment)
    service.runs.complete(run["id"], {"type": "assessment", "id": assessment["id"]})
    return {"assessment_id": assessment["id"], "question_id": question["id"],
            "review_id": event["review_id"], "event_id": event["id"], "run_id": run["id"],
            "review_version": event["review_version"], "state_version": event["state_version"],
            "status": event["question_status"]}


def report_question(service, assessment_id, payload, key=None):
    payload = QuestionReport.model_validate(payload).model_dump()

    def change():
        assessment = _assessment(service, assessment_id)
        _check_version(assessment, payload)
        question = _question(assessment, payload["question_id"])
        previous_status = effective_question(assessment, question)["question_review_status"]
        if previous_status == "pending":
            raise DomainConflict("QUESTION_REVIEW_PENDING", "题目已有待裁决复核")
        review_id = str(uuid4())
        event = {"id": review_id, "review_id": review_id, "action": "report",
                 "question_id": question["id"], "reason": payload["reason"], "created_at": now(),
                 "previous_status": previous_status, "question_status": "pending",
                 "scope": "assessment_question_snapshot"}
        return _publish(service, assessment, question, event)

    return service._execute("assessment.question_report", assessment_id, payload, key, change)


def resolve_question_review(service, assessment_id, review_id, payload, key=None):
    payload = QuestionResolution.model_validate(payload).model_dump()

    def change():
        assessment = _assessment(service, assessment_id)
        _check_version(assessment, payload)
        events = assessment["snapshot"].get("question_review_events", [])
        report = next((e for e in events if e["id"] == review_id and e["action"] == "report"), None)
        if report is None:
            raise DomainNotFound("question_review", review_id)
        if any(e["review_id"] == review_id and e["action"] != "report" for e in events):
            raise DomainConflict("QUESTION_REVIEW_RESOLVED", "复核已有不可变裁决，请创建新的复核")
        question = _question(assessment, report["question_id"])
        serialize = lambda ref: json.dumps(ref, sort_keys=True, ensure_ascii=False)
        allowed = {serialize(ref) for ref in question["source_refs"]}
        proof = [serialize(ref) for ref in payload["source_refs"]]
        if len(set(proof)) != len(proof) or not set(proof) <= allowed or not _source_text(assessment, question).strip():
            raise DomainConflict("INVALID_SOURCE_PROOF", "裁决必须引用此题的封存来源并核对原文")
        event_id = str(uuid4())
        action = payload["action"]
        event = {"id": event_id, "review_id": review_id, "question_id": question["id"],
                 "action": action, "reason": payload["reason"], "source_refs": deepcopy(payload["source_refs"]),
                 "confirmed": True, "adjudicator": "local_user", "created_at": now(),
                 "scope": "assessment_question_snapshot",
                 "question_status": {"correct": "corrected", "invalidate": "invalid", "reject": report["previous_status"]}[action]}
        if action == "correct":
            answer_key = payload["corrected_answer_key"]
            if question["type"] == "single_choice" and answer_key not in {o["id"] for o in question["options"]}:
                raise DomainConflict("INVALID_CORRECTION", "客观题更正答案必须是现有有效选项 ID")
            event["correction"] = {"answer_key": answer_key, "rubric": payload["corrected_rubric"],
                                   "rubric_version": "human-" + event_id}
        return _publish(service, assessment, question, event)

    command = {"review_id": review_id, **payload}
    return service._execute("assessment.question_resolution", assessment_id, command, key, change)


def question_reviews(service, assessment_id):
    access = SourceAccessService(service)
    for attempt in range(3):
        snapshot = _assessment(service, assessment_id, lock=False)
        reported = {e["question_id"] for e in snapshot["snapshot"].get("question_review_events", [])}
        topics = {q["topic_id"] for q in snapshot["questions"] if q["id"] in reported}
        # The displayed frozen text includes the whole topic, so all its source
        # refs participate, even when the reviewed question cited only a subset.
        refs = [ref for topic in snapshot["snapshot"]["topics"] if topic["id"] in topics for ref in topic["source_refs"]]
        plan = access.prepare_delivery(refs, snapshot["space_id"])
        try:
            with service.repository.transaction():
                # Match chat/source disclosure lock order. The completed owner
                # is read without locking after active assessment/space locks.
                active, _ = access.lock_delivery(plan, snapshot["space_id"])
                assessment = _assessment(service, assessment_id, lock=False)
                if _version(assessment) != _version(snapshot):
                    raise AssistanceDeliveryChanged()
                access.record_delivery(active, plan, kind="question_review",
                                       delivery_id=str(uuid4()), space_id=snapshot["space_id"])
                return _review_view(assessment)
        except AssistanceDeliveryChanged:
            if attempt == 2:
                raise DomainConflict("VERSION_CONFLICT", "复核或活动测验已变化，请重新读取") from None
    raise AssertionError("unreachable")


def _review_view(assessment):
    events = assessment["snapshot"].get("question_review_events", [])
    items = []
    for report in events:
        if report["action"] != "report":
            continue
        question = _question(assessment, report["question_id"])
        history = [deepcopy(e) for e in events if e["review_id"] == report["id"]]
        items.append({"review_id": report["id"], "question_id": question["id"],
                      "scope": "assessment_question_snapshot", "status": history[-1]["question_status"],
                      "events": history, "question": effective_question(assessment, question),
                      "frozen_question": deepcopy(question), "source_text": _source_text(assessment, question)})
    return {"assessment_id": assessment["id"], "review_version": _version(assessment), "items": items}
