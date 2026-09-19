"""Source reads cannot silently turn assisted answers into independent evidence."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.materials.service import MaterialService, InMemoryMaterialRepository
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions


@pytest.fixture(params=["memory", "sql"])
def source_workspace(request, tmp_path):
    engine = None
    if request.param == "sql":
        engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'sources.db').as_posix()}")
        init_db(engine)
        repo = SqlAlchemyMaterialRepository(engine)
    else:
        repo = InMemoryMaterialRepository()
    service = MaterialService(repo)
    result = service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="material")
    def factory():
        return create_app(service, question_generator=FixedQuestions())
    app = factory()
    state = app.state.learning_state
    space = state.create_space({"name": "Study", "material_ids": [result.material.id]})
    path = f"/api/v1/materials/{result.material.id}/versions/{result.version.id}/chunks/{result.version.chunks[0].id}"
    with TestClient(app) as client:
        yield client, state, space, result, path, factory
    if engine:
        engine.dispose()


def assessment(state, space, dispatch=None):
    return state.create_assessment(space["id"], {"question_count": 5, "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}}, dispatch=dispatch)


@pytest.mark.parametrize("whole_material", [False, True])
def test_active_assessment_requires_space_context(source_workspace, whole_material):
    client, state, space, material, path, _ = source_workspace
    assert client.get(path).status_code == 200
    created = assessment(state, space)
    if whole_material:
        path = f"/api/v1/materials/{material.material.id}"
    denied = client.get(path)
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "SPACE_CONTEXT_REQUIRED"
    assert client.get(path, params={"space_id": space["id"]}).status_code == 200
    stored = state.assessment_service.repository.get_record("assessments", created["id"])
    assert all(q.get("assisted") for q in stored["questions"])


def test_source_consultation_survives_restart_and_prevents_mastery_credit(source_workspace):
    client, state, space, material, path, factory = source_workspace
    created = assessment(state, space)
    questions = state.get_assessment(created["id"])["questions"]
    state.record_attempt(created["id"], {"answers": [{"question_id": q["id"], "answer": "A", "expected_answer_revision": 0} for q in questions]}, idempotency_key="answer")
    assert client.get(path, params={"space_id": space["id"]}).status_code == 200
    with TestClient(factory()) as restarted:
        result = restarted.post(f"/api/v1/assessments/{created['id']}/finalize", json={})
        assert result.status_code == 202
        evidence = restarted.get(f"/api/v1/learning-spaces/{space['id']}/evidence").json()["items"]
        assert len(evidence) == 5
        assert all(e["assisted"] and not e["eligible"] for e in evidence)
    # A finished assessment no longer prevents an ordinary source read.
    assert client.get(path).status_code == 200


def test_generation_remembers_sources_read_before_questions_exist(source_workspace):
    client, state, space, _, path, _ = source_workspace
    queued = []
    created = assessment(state, space, dispatch=lambda fn, *args: queued.append((fn, args)))
    assert client.get(path, params={"space_id": space["id"]}).status_code == 200
    fn, args = queued[0]
    fn(*args)
    stored = state.assessment_service.repository.get_record("assessments", created["id"])
    assert stored["status"] == "ready"
    assert all(q.get("assisted") for q in stored["questions"])


def test_other_space_cannot_hide_same_users_assistance(source_workspace):
    client, state, space, material, path, _ = source_workspace
    other = state.create_space({"name": "Other", "material_ids": [material.material.id]})
    created = assessment(state, space)
    assert client.get(path, params={"space_id": other["id"]}).status_code == 200
    stored = state.assessment_service.repository.get_record("assessments", created["id"])
    assert all(q.get("assisted") for q in stored["questions"])


def test_unbound_or_unknown_space_is_rejected(source_workspace):
    client, state, _, _, path, _ = source_workspace
    assert client.get(path, params={"space_id": "missing"}).status_code == 404
    different = state.material_service.create(filename="other.md", content=b"# Different\n\nOther facts", idempotency_key="different")
    other = state.create_space({"name": "Other", "material_ids": [different.material.id]})
    denied = client.get(path, params={"space_id": other["id"]})
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "SOURCE_OUT_OF_SCOPE"


def test_interrupted_generation_does_not_permanently_block_sources(source_workspace):
    client, state, space, _, path, _ = source_workspace
    created = assessment(state, space, dispatch=lambda *args: None)
    state.run_service.recover_interrupted()
    assert state.get_assessment(created["id"])["status"] == "failed"
    assert client.get(path).status_code == 200
