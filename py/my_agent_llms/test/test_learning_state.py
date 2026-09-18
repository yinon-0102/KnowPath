from my_agent_llms.test.test_learning_state_persistence import FixedQuestions
from my_agent_llms.learning.materials import InMemoryMaterialRepository, MaterialService
from my_agent_llms.learning.state import LearningState


def _state_with_material() -> tuple[LearningState, str]:
    state = LearningState(question_generator=FixedQuestions())
    material = state.material_service.create(
        filename="python.md",
        content=b"# Functions\n\nFunctions group reusable behavior.\n\n## Parameters\n\nParameters receive values.",
        idempotency_key="material-1",
    )
    return state, material.material.id


def test_learning_space_binds_material_and_scope():
    state, material_id = _state_with_material()

    space = state.create_space({"name": "Python", "material_ids": [material_id], "goal": "Use functions"})
    scoped = state.set_scope(space["id"], {"topic_ids": ["topic_functions"], "expected_version": 1})

    assert space["bindings"][0]["material_id"] == material_id
    assert scoped["scope_version"] == 1
    assert scoped["topic_ids"] == ["topic_functions"]


def test_assessment_finalize_updates_state_once():
    state, material_id = _state_with_material()
    space = state.create_space({"name": "Python", "material_ids": [material_id], "goal": "Use functions"})
    state.set_scope(space["id"], {"topic_ids": ["topic_functions"], "expected_version": 1})
    assessment = state.create_assessment(space["id"], {"kind": "diagnostic", "question_count": 5, "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}})
    assessment = state.get_assessment(assessment["id"])
    question_id = assessment["questions"][0]["id"]

    state.record_attempt(assessment["id"], {"answers": [{"question_id": question_id, "answer": "A", "expected_answer_revision": 0}]})
    first = state.finalize_assessment(assessment["id"], {"allow_unanswered": True})
    second = state.finalize_assessment(assessment["id"], {"allow_unanswered": True})

    assert first["run_id"] == second["run_id"]
    assert state.get_state(space["id"])["state_version"] == 1
    assert len(state.evidence_for(space["id"])) == 5


def test_plan_and_session_lifecycle():
    state, material_id = _state_with_material()
    space = state.create_space({"name": "Python", "material_ids": [material_id], "goal": "Use functions"})
    state.set_scope(space["id"], {"topic_ids": ["topic_functions"], "expected_version": 1})

    plan = state.create_plan(space["id"], {"session_count": 3, "minutes_per_session": 30})
    session = state.start_session(plan["plan_id"], plan["tasks"][0]["id"])
    state.add_session_event(session["id"], {"type": "open_material"})
    finished = state.finish_session(session["id"])

    assert plan["tasks"]
    assert finished["status"] == "finished"
