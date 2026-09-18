"""Atomic grade review using the original question, rubric and submitted answer."""
from copy import deepcopy
from uuid import uuid4

from .assessment_grading import grade_answer
from .assessment_schemas import GradeReview
from .errors import DomainConflict, DomainNotFound
from .spaces import now


def review_grade(service, assessment_id, payload, key=None):
    payload = GradeReview.model_validate(payload).model_dump()

    def change():
        # Shared order with finalization: assessment -> space -> evidence/state.
        assessment = service.repository.get_record("assessments", assessment_id)
        if assessment["status"] != "completed" or assessment.get("result") is None:
            raise DomainConflict("ASSESSMENT_NOT_READY", "测验尚未完成评分，不能复核")
        question = next((q for q in assessment["questions"] if q["id"] == payload["question_id"]), None)
        if question is None:
            raise DomainNotFound("question", payload["question_id"])
        space = service.spaces.repository.get(assessment["space_id"])
        result = deepcopy(assessment["result"])
        previous_result = next(q for q in result["question_results"] if q["question_id"] == question["id"])
        old = service.repository.get_record("evidence", previous_result["evidence_id"])
        # Never select a new answer revision or fetch a regenerated question.
        answer = service.repository.get_record("attempts", old["attempt_id"]) if old.get("attempt_id") else None
        score, verdict, feedback = grade_answer(question, answer)
        timestamp, review_id = now(), str(uuid4())
        run = service.runs.create("grade_review", status="running")
        current_topics = {t["id"]: t["revision_id"] for t in service._topics(dict(space, topic_ids=[], excluded_topic_ids=[]))}
        epochs = service._epochs(space["id"])
        replacement = deepcopy(old)
        replacement.pop("revoked_by_review_id", None)
        replacement.pop("revoked_at", None)
        replacement.update(id=str(uuid4()), score=score, result=verdict,
            review_id=review_id, reviewed_at=timestamp, replaces_evidence_id=old["id"],
            observation_id=old.get("observation_id", old["id"]),
            error_tags=["incorrect_answer"] if verdict == "incorrect" else [],
            eligible=(score is not None and not old.get("assisted")
                      and old["epoch"] == epochs.get(old["topic_id"], 0)
                      and old["topic_revision_id"] == current_topics.get(old["topic_id"])))
        # Keep observation time/family/submission unchanged: a review is not a
        # new independent answer or a later spaced assessment.
        old.update(eligible=False, revoked_by_review_id=review_id, revoked_at=timestamp)
        service.repository.put_record("evidence", old)
        service.repository.put_record("evidence", replacement)
        reviewed = {**previous_result, "score": score, "verdict": verdict, "feedback": feedback,
                    "evidence_id": replacement["id"], "review_id": review_id}
        result["question_results"] = [reviewed if q["question_id"] == question["id"] else q for q in result["question_results"]]
        current_ids = {q["evidence_id"] for q in result["question_results"]}
        current_records = [e for e in service.repository.records("evidence", assessment_id=assessment_id) if e["id"] in current_ids]
        for topic_result in result["topic_results"]:
            rows = [e for e in current_records if e["topic_id"] == topic_result["topic_id"]]
            verified = [e for e in rows if e["score"] is not None]
            topic_result.update(score=sum(e["score"] for e in verified) / len(verified) if verified else None,
                verified_count=len(verified), unverified_count=len(rows) - len(verified),
                error_tags=sorted({tag for e in rows for tag in e["error_tags"]}), evidence_ids=[e["id"] for e in rows])
        version = space["state_version"] + 1
        topic_id = old["topic_id"]
        if old["epoch"] == epochs.get(topic_id, 0) and old["topic_revision_id"] == current_topics.get(topic_id):
            prior = service.repository.records("states", space_id=space["id"], topic_id=topic_id)
            assessed_at = prior[0].get("last_assessed_at") if prior else None
            service._recompute(space["id"], topic_id, current_topics[topic_id], old["epoch"], version, assessed_at or old["created_at"])
        space["state_version"] = version
        service.spaces.repository.put(space)
        result.update(state_version=version, reviewed_at=timestamp)
        assessment["result"] = result
        assessment.setdefault("grade_reviews", []).append({"id": review_id, "run_id": run["id"],
            "question_id": question["id"], "reason": payload["reason"], "created_at": timestamp,
            "previous_result": previous_result, "result": reviewed,
            "replaced_evidence_id": old["id"], "evidence_id": replacement["id"]})
        service.repository.put_record("assessments", assessment)
        service.runs.complete(run["id"], {"type": "assessment", "id": assessment_id})
        return {"run_id": run["id"], "review_id": review_id, "assessment_id": assessment_id, "status": "succeeded"}

    return service._execute("assessment.grade_review", assessment_id, payload, key, change)
