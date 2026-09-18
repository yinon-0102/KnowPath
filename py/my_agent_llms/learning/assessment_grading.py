"""Conservative scoring of frozen questions and immutable submitted answers."""


def grade_answer(question, answer):
    if answer is None:
        return None, "unverified", "insufficient_evidence: 未作答"
    if question["type"] == "single_choice":
        score = 1.0 if answer["answer"] == question["answer_key"] else 0.0
        return score, "correct" if score else "incorrect", "已按封存客观答案判分"
    return None, "unverified", "开放题尚无可靠判分，保存反馈并建议替代客观题"
