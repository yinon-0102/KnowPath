"""Conservative scoring of frozen questions and immutable submitted answers."""
from copy import deepcopy


def effective_question(assessment, question):
    """Replay confirmed local revisions; never mutate the generated snapshot."""
    effective = deepcopy(question)
    effective["question_review_status"] = "accepted"
    for event in assessment["snapshot"].get("question_review_events", []):
        if event["question_id"] != question["id"]:
            continue
        if event["action"] == "correct":
            effective.update(event["correction"])
        effective["question_review_status"] = event["question_status"]
    return effective


def grade_answer(question, answer):
    if question.get("question_review_status") in {"pending", "invalid"}:
        return None, "unverified", "题目已隔离，等待人工确认或已作废，不计入掌握度"
    if answer is None:
        return None, "unverified", "insufficient_evidence: 未作答"
    if question["type"] == "single_choice":
        score = 1.0 if answer["answer"] == question["answer_key"] else 0.0
        return score, "correct" if score else "incorrect", "已按封存客观答案判分"
    return None, "unverified", "开放题尚无可靠判分，保存反馈并建议替代客观题"
