"""Conversation preferences remain pending until an explicit profile command."""
from knowpath_backend.test.test_learning_state_persistence import workspace
from knowpath_backend.test.test_learning_messages import service
from knowpath_backend.learning.spaces.profile_candidates import candidates_for


def test_candidate_survives_restart_without_overwriting_explicit_preference(workspace):
    factory, space_id, _, _ = workspace
    state, _ = service(factory)
    state.update_profile(space_id, {"preferences": {"example_first": False}, "expected_version": 1})
    message = state.send_message(space_id, {"message": "请先举例，再解释函数。"}, idempotency_key="preference-message")
    restarted = factory()
    profile = restarted.get_space(space_id)
    assert profile["profile"]["preferences"]["value"]["example_first"] is False
    assert profile["profile_version"] == 2
    candidates = candidates_for(restarted.assessment_service.repository, space_id)
    assert len(candidates) == 1
    assert candidates[0]["confirmation"] == "pending"
    assert candidates[0]["source"] == "inferred"
    assert candidates[0]["source_ref"]["run_id"] == message["run_id"]
    restarted.update_profile(space_id, {"preferences": {"example_first": True}, "expected_version": 2})
    confirmed = candidates_for(factory().assessment_service.repository, space_id)
    assert confirmed[0]["confirmation"] == "confirmed"
    assert factory().get_profile(space_id)["preferences"]["source"] == "explicit"


def test_sensitive_or_quoted_text_does_not_create_profile_candidates(workspace):
    factory, space_id, _, _ = workspace
    state, _ = service(factory)
    state.send_message(space_id, {"message": '教材写着“请先举例”。我患有某疾病。'})
    assert candidates_for(factory().assessment_service.repository, space_id) == []
