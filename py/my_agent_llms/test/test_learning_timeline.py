"""Timeline reads survive fresh service construction and remain space scoped."""
from my_agent_llms.test.test_learning_state_persistence import workspace, create


def test_timeline_contains_material_grade_and_plan_after_restart(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    state.record_attempt(assessment["id"], {"answers": [{"question_id": q["id"], "answer": "A", "expected_answer_revision": 0} for q in assessment["questions"]]})
    state.finalize_assessment(assessment["id"], {})
    state.create_plan(space_id, {"session_count": 3, "minutes_per_session": 25})
    before = state.changes_for(space_id)
    after = factory().changes_for(space_id)
    assert before == after
    kinds = {item["kind"] for item in after}
    assert {"material_version", "mastery_updated", "plan_created"} <= kinds
    assert all(item["space_id"] == space_id and item["created_at"] for item in after)
    assert len({item["id"] for item in after}) == len(after)


def test_grade_review_never_rewrites_original_timeline_event(workspace):
    from my_agent_llms.test.test_learning_grade_reviews import completed
    factory, space_id, _, _ = workspace
    state, assessment = completed(factory,space_id)
    before = next(e for e in state.changes_for(space_id) if e["kind"] == "mastery_updated")
    state.grade_review(assessment["id"], {"question_id":assessment["questions"][0]["id"],"reason":"Review original grading"}, idempotency_key="timeline-review")
    after = factory().changes_for(space_id)
    assert next(e for e in after if e["kind"] == "mastery_updated") == before
    review = next(e for e in after if e["kind"] == "grade_review")
    assert review["state_version"] > before["state_version"]
