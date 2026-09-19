"""Durable, safe and atomic export snapshots."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from zipfile import ZipFile

import pytest
from sqlalchemy import create_engine, select, func
from knowpath_backend.learning.db import init_db, RunRow
from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.exports import ExportService
from knowpath_backend.learning.materials import InMemoryMaterialRepository
from knowpath_backend.learning.repositories import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState


@pytest.fixture(params=["memory", "sqlite"])
def workspace(request, tmp_path):
    engine = None
    if request.param == "sqlite":
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'exports.db'}")
        init_db(engine)
        factory = lambda: LearningState(SqlAlchemyMaterialRepository(engine))
    else:
        materials = InMemoryMaterialRepository()
        factory = lambda: LearningState(materials)
    state = factory()
    material = state.material_service.create(filename="../../private.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="material")
    space = state.create_space({"name": "Python", "goal": "Learn functions", "material_ids": [material.material.id], "weekly_minutes": 120})
    topic = state.topics_for_space(space["id"])[0]
    state.set_scope(space["id"], {"topic_ids": [topic["id"]], "expected_version": 1})
    yield factory, state, space["id"], topic, engine
    if engine is not None:
        engine.dispose()


def service(state, clock=None):
    kwargs = {"clock": clock} if clock else {}
    return ExportService(state.assessment_service.repository, state.space_service,
                         state.assessment_service, state.plan_sessions, state.run_service, **kwargs)


def run_count(state, engine):
    if engine is not None:
        with engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(RunRow))
    return len(state.run_service.repository._runs)


def test_export_snapshot_zip_and_run_survive_restart(workspace):
    factory, state, space_id, topic, _ = workspace
    plan = state.create_plan(space_id, {})
    session = state.start_session(plan["id"], plan["tasks"][0]["id"])
    created = service(state).create(space_id, {"format": "json"}, "export-1")
    assert created["status"] == "ready"
    restored = factory()
    exports = service(restored)
    payload = exports.payload(created["export_id"])
    assert payload["space"]["goal"] == "Learn functions"
    assert payload["plans"][0]["id"] == plan["id"]
    assert payload["sessions"][0]["id"] == session["id"]
    assert payload["versions"]["materials"][0]["material_version_id"] == topic["source_refs"][0]["material_version_id"]
    assert payload["source_refs"][0]["chunk_id"] == topic["source_refs"][0]["chunk_id"]
    assert restored.get_run(created["run_id"])["status"] == "succeeded"
    assert restored.get_run(created["run_id"])["result_ref"] == {"type": "export", "id": created["export_id"]}
    state.update_profile(space_id, {"goal": "Changed", "expected_version": 1})
    payload["space"]["goal"] = "Mutated copy"
    with ZipFile(BytesIO(exports.archive(created["export_id"]))) as archive:
        assert archive.namelist() == ["learning-space.json"]
        assert json.loads(archive.read("learning-space.json"))["space"]["goal"] == "Learn functions"
    assert exports.create(space_id, {"format": "json"}, "export-1") == created


def test_export_expires_exactly_after_twenty_four_hours(workspace):
    _, state, space_id, _, _ = workspace
    timestamp = datetime(2026, 9, 19, tzinfo=timezone.utc)
    exports = service(state, lambda: timestamp)
    result = exports.create(space_id, {}, "expires")
    timestamp += timedelta(hours=24) - timedelta(microseconds=1)
    assert exports.archive(result["export_id"]).startswith(b"PK")
    timestamp += timedelta(microseconds=1)
    for reader in (exports.payload, exports.archive):
        with pytest.raises(DomainConflict) as error:
            reader(result["export_id"])
        assert error.value.code == "EXPORT_EXPIRED"


def test_export_is_atomic_with_run_and_idempotency(workspace, monkeypatch):
    factory, state, space_id, _, engine = workspace
    exports = service(state)
    before = run_count(state, engine)
    original = exports.repository.remember
    def fail(*args, **kwargs):
        raise RuntimeError("idempotency write failed")
    monkeypatch.setattr(exports.repository, "remember", fail)
    with pytest.raises(RuntimeError, match="idempotency write failed"):
        exports.create(space_id, {}, "atomic")
    assert run_count(state, engine) == before
    if engine is None:
        assert state.material_repository.export_data == {}
    else:
        from knowpath_backend.learning.db import ExportRow
        with engine.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(ExportRow)) == 0
    monkeypatch.setattr(exports.repository, "remember", original)
    assert service(factory()).create(space_id, {}, "atomic")["status"] == "ready"


def test_export_unknown_id_validation_and_idempotency_conflict(workspace):
    _, state, space_id, _, _ = workspace
    exports = service(state)
    for identifier in ("missing", "../../private.md"):
        with pytest.raises(DomainNotFound):
            exports.archive(identifier)
    for body in ({"format": "csv"}, {"path": "../../private"}):
        with pytest.raises(DomainConflict) as error:
            exports.create(space_id, body, "invalid")
        assert error.value.code == "INVALID_REQUEST"
    exports.create(space_id, {}, "shared-key")
    other = state.create_space({"name": "Other", "material_ids": [state.get_space(space_id)["bindings"][0]["material_id"]]})
    with pytest.raises(DomainConflict) as error:
        exports.create(other["id"], {}, "shared-key")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_export_whitelist_excludes_internal_context_and_pending_answers(workspace):
    _, state, space_id, topic, _ = workspace
    repo = state.assessment_service.repository
    space = state.space_service.get(space_id)
    space["api_key"] = "SPACE_SECRET"
    space["profile"]["system_prompt"] = {"value": "PROFILE_SECRET"}
    space["profile"]["goal"]["system_prompt"] = "PROFILE_ENTRY_SECRET"
    state.space_service.repository.put(space)
    timestamp = datetime.now(timezone.utc).isoformat()
    repo.put_record("assessments", {"id": "assessment", "space_id": space_id, "kind": "diagnostic", "status": "ready", "topic_ids": [topic["id"]], "questions": [{"id": "question", "answer": "UNSUBMITTED_SECRET"}], "snapshot": {"api_key": "SNAPSHOT_SECRET"}, "result": None, "created_at": timestamp})
    repo.put_record("states", {"id": "state", "space_id": space_id, "topic_id": topic["id"], "mastery_score": 0.8, "score_validity": "valid", "status": "learning", "evidence_ids": ["evidence"], "error_tags": [], "state_version": 1, "policy_version": "v1", "last_assessed_at": None, "next_review_at": None, "system_prompt": "STATE_SECRET"})
    repo.put_record("evidence", {"id": "evidence", "space_id": space_id, "topic_id": topic["id"], "assessment_id": None, "attempt_id": None, "kind": "assessment", "result": "correct", "score": 0.8, "error_tags": [], "source_refs": [dict(topic["source_refs"][0], api_key="REF_SECRET"), {"material_id": "other-space", "material_version_id": "other-version", "chunk_id": "other-chunk"}], "created_at": timestamp, "api_key": "EVIDENCE_SECRET"})
    plan = state.create_plan(space_id, {})
    session = state.start_session(plan["id"], plan["tasks"][0]["id"])
    state.add_session_event(session["id"], {"type": "open_material", "event_id": "event-1", "system_prompt": "EVENT_SECRET"})
    exports = service(state)
    payload = exports.payload(exports.create(space_id, {}, "safe")["export_id"])
    serialized = json.dumps(payload)
    assert "SECRET" not in serialized
    assert "other-space" not in serialized
    assert "questions" not in serialized
    assert payload["space"]["state"][topic["id"]]["mastery_score"] == 0.8
    assert payload["evidence"][0]["score"] == 0.8
    assert payload["sessions"][0]["events"][0]["type"] == "open_material"


def test_concurrent_same_key_creates_one_export_and_one_run(workspace):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    factory, state, space_id, _, engine = workspace
    before = run_count(state, engine)
    barrier = Barrier(2)
    def create():
        exports = service(factory())
        barrier.wait(timeout=10)
        return exports.create(space_id, {}, "concurrent")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1]
    assert run_count(state, engine) == before + 1


def test_export_http_download_replay_and_expiry():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    app = create_app()
    state = app.state.learning_state
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="material")
    space = state.create_space({"name": "Python", "material_ids": [material.material.id]})
    url = f"/api/v1/learning-spaces/{space['id']}/exports"
    headers = {"Idempotency-Key": "export-http"}
    with TestClient(app) as client:
        assert client.post(url, json={}).status_code == 400
        assert client.post(url, json={"format": "xml"}, headers=headers).status_code == 422
        assert client.post(url, json={"format": "json", "private": True}, headers=headers).status_code == 422
        first = client.post(url, json={"format": "json"}, headers=headers)
        assert first.status_code == 202
        assert first.json()["status"] == "ready"
        assert client.post(url, json={"format": "json"}, headers=headers).json() == first.json()
        download = f"/api/v1/exports/{first.json()['export_id']}/download"
        response = client.get(download)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]
        with ZipFile(BytesIO(response.content)) as archive:
            assert archive.namelist() == ["learning-space.json"]
            assert json.loads(archive.read("learning-space.json"))["space"]["id"] == space["id"]
        state.export_service.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=25)
        assert client.get(download).status_code == 410
        assert client.get("/api/v1/exports/missing/download").status_code == 404


def test_deleting_export_only_space_cleans_exports(workspace):
    _, state, space_id, _, _ = workspace
    response = service(state).create(space_id, {}, "delete-export")
    state.delete_space(space_id, {"confirm": True, "expected_version": state.get_space(space_id)["space_version"]})
    with pytest.raises(DomainNotFound):
        service(state).payload(response["export_id"])


def test_export_preserves_review_audit_and_mastery_counts(workspace):
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions, create, answer
    _, state, space_id, _, _ = workspace
    state.assessment_service.generator = FixedQuestions()
    assessment = create(state, space_id)
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    state.grade_review(assessment["id"], {"question_id": assessment["questions"][0]["id"], "reason": "核查"})
    payload = state.export_payload(state.create_export(space_id)["export_id"])
    current = next(iter(payload["space"]["state"].values()))
    assert current["evidence_count"] == 5
    assert current["independent_evidence_count"] == 5
    replacement = next(e for e in payload["evidence"] if e.get("replaces_evidence_id"))
    old = next(e for e in payload["evidence"] if e["id"] == replacement["replaces_evidence_id"])
    assert old["revoked_by_review_id"] == replacement["review_id"]
    assert replacement["observation_id"] == old["id"]
