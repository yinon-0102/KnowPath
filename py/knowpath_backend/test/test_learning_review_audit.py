"""Review audit must survive SQL serialization and service restarts."""
from knowpath_backend.test.test_learning_state_persistence import workspace, create, answer


def test_review_audit_preserves_reason_and_every_revision(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    question_id = assessment["questions"][0]["id"]
    first = state.grade_review(assessment["id"], {"question_id": question_id, "reason": "first audit reason"})
    second = factory().grade_review(assessment["id"], {"question_id": question_id, "reason": "second audit reason"})
    record = factory().assessment_service.repository.get_record("assessments", assessment["id"])
    audit = record["grade_reviews"]
    assert [row["id"] for row in audit] == [first["review_id"], second["review_id"]]
    assert [row["reason"] for row in audit] == ["first audit reason", "second audit reason"]
    assert audit[1]["replaced_evidence_id"] == audit[0]["evidence_id"]
    assert [row["run_id"] for row in audit] == [first["run_id"], second["run_id"]]
    assert all(row["question_id"] == question_id for row in audit)
