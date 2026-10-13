"""Disclosure restricted to immutable answers already accepted by the server."""
from copy import deepcopy
import re
from .grading import effective_question, grade_answer


def readable_explanation(question):
    text = question.get('rubric', '')
    options = {str(o['id']): '“' + o['text'] + '”' for o in question.get('options', [])}
    if not options:
        return text
    pattern = r'(?<![A-Za-z0-9_])(?:' + '|'.join(re.escape(k) for k in sorted(options, key=len, reverse=True)) + r')(?![A-Za-z0-9_])'
    return re.sub(pattern, lambda match: options[match[0]], text)


def answer_feedback(assessment, question, answer):
    question = effective_question(assessment, question)
    score, verdict, feedback = grade_answer(question, answer)
    verified = question.get("question_review_status") not in {"pending", "invalid"}
    return {"question_id": question["id"], "answer_revision": answer["answer_revision"],
            "answer": answer["answer"], "verdict": verdict, "score": score,
            "feedback": feedback, "correct_answer": question.get("answer_key") if verified else None,
            "explanation": readable_explanation(question) if verified else feedback,
            "source_refs": deepcopy(question["source_refs"]), "assisted": bool(answer.get("assisted"))}


def record_feedback_exposure(assessment, answered, answers):
    """Later questions sharing disclosed sources are no longer independent."""
    sources = {(r["material_version_id"], r["chunk_id"]) for q in answered for r in q["source_refs"]}
    for question in assessment["questions"]:
        if question["id"] not in answers and any((r["material_version_id"], r["chunk_id"]) in sources
                                                  for r in question["source_refs"]):
            question["assisted"] = True


def deliver_feedback_exposure(service, assessment_id):
    """Fence every related assessment before returning or restoring answers."""
    from knowpath_backend.learning.materials.source_access import SourceAccessService, AssistanceDeliveryChanged
    from knowpath_backend.learning.errors import DomainConflict
    access = SourceAccessService(service)
    assessment = service.repository.get_record('assessments', assessment_id)
    disclosed = set(assessment['snapshot'].get('feedback_question_ids', []))
    refs = [r for q in assessment['questions'] if q['id'] in disclosed for r in q['source_refs']]
    if not refs:
        return
    # Run outside the owning assessment command to preserve global lock order.
    # A replay repairs delivery if the first response was interrupted.
    delivery_id = assessment_id + ':' + ','.join(sorted(disclosed))
    for _ in range(3):
        plan = access.prepare_delivery(refs, assessment['space_id'])
        try:
            with service.repository.transaction():
                active, _ = access.lock_delivery(plan, assessment['space_id'])
                access.record_delivery(active, plan, kind='question_feedback', delivery_id=delivery_id,
                                       space_id=assessment['space_id'], preserve_submitted=True)
            return
        except AssistanceDeliveryChanged:
            continue
    raise DomainConflict('STALE_LEARNING_CONTEXT', '学习范围已变化，请重新读取解析')
