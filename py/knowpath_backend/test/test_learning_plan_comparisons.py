"""Budget previews are feasible, auditable, durable and business-state read-only."""
import copy
import importlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.knowledge.reconciliation import extract_snapshot, digest
from knowpath_backend.learning.persistence.db import Base
from knowpath_backend.learning.state import LearningState
from knowpath_backend.test.test_learning_plans_sessions import backend

NOW = "2026-09-26T12:00:00+00:00"
REQUEST = {"budgets_minutes_per_day": [20, 40], "horizon_days": 1}


def module():
    assert importlib.util.find_spec("knowpath_backend.learning.plans.comparison") is not None, "Budget comparison service has not been implemented"
    return importlib.import_module("knowpath_backend.learning.plans.comparison")


def service(state, monkeypatch):
    implementation = module()
    monkeypatch.setattr(implementation, "now", lambda: NOW)
    return implementation.PlanComparisonService(state.plan_sessions)


def setup(repo, count=8, edges=(), *, weekly=120, target=None):
    state = LearningState(repo)
    content = "\n\n".join(f"# Topic {i:03}\n\nDefinition and grounded material for topic {i}." for i in range(count))
    upload = state.material_service.create(filename="comparison.md", content=content.encode(), idempotency_key=uuid4().hex)
    graph = extract_snapshot(upload.material.id, upload.version)
    topics = sorted(graph["nodes"], key=lambda t: t["name"])
    for index, entry in enumerate(edges):
        parent, child, status, confirmed = entry if len(entry) == 4 else (*entry, "active", True)
        graph["relations"].append({"id": f"edge-{index}", "type": "prerequisite_of",
            "from_id": topics[parent]["id"], "to_id": topics[child]["id"],
            "status": status, "confirmed": confirmed, "source_refs": topics[child]["source_refs"]})
    identifier = str(uuid4())
    row = {"id": identifier, "material_id": upload.material.id, "material_version_id": upload.version.id,
        "sequence": 1, "base_graph_version": 0, "graph_version": 1, "status": "published",
        "run_id": str(uuid4()), "snapshot": graph, "snapshot_hash": digest(graph), "diff": {},
        "preparation": None, "publication": {"snapshot": graph}, "created_at": NOW}
    with state.graph_service.repository.transaction():
        state.graph_service.repository.put_record("graph_revisions", row)
    space = state.create_space({"name": "Budget preview", "material_ids": [upload.material.id],
        "weekly_minutes": weekly, "target_date": target})
    state.set_scope(space["id"], {"topic_ids": [t["id"] for t in topics], "expected_version": 1})
    return state, space["id"], topics


def business_snapshot(state):
    repository = state.material_repository
    if not getattr(repository, "unit_of_work", None):
        return copy.deepcopy({"spaces": repository.learning_spaces, "data": repository.assessment_data,
            "runs": {k: v for k, v in state.run_service.repository.__dict__.items()
                     if isinstance(v, (dict, list, str, int, float, bool, type(None)))}})
    with repository.unit_of_work.engine.connect() as connection:
        return {table.name: sorted((dict(row) for row in connection.execute(select(table)).mappings()), key=repr)
            for table in Base.metadata.sorted_tables if table.name != "idempotency_keys"}


def test_budgets_share_snapshot_choose_subsets_and_preserve_all_business_rows(backend, monkeypatch):
    state, space_id, _ = setup(backend)
    compare = service(state, monkeypatch)
    before = business_snapshot(state)
    result = compare.compare(space_id, REQUEST, idempotency_key="preview")
    low, high = result["scenarios"]
    assert low["summary"]["selected_topic_count"] == 2
    assert high["summary"]["selected_topic_count"] == 4
    assert low["summary"]["unknown_topics_covered"] == 2
    assert high["summary"]["coverage_fraction"] > low["summary"]["coverage_fraction"]
    assert len(result["snapshot"]["hash"]) == 64
    assert result["strategy_version"] == "budget-closure-v1"
    assert low["deferred"] and low["deferred"][0]["reason_codes"]
    assert business_snapshot(state) == before
    assert all("unknown_state" in item["reason_codes"] for item in high["schedule"])


def test_confirmed_dependencies_precede_dependents_and_unconfirmed_edges_do_not_block(backend, monkeypatch):
    state, space_id, topics = setup(backend, count=4, edges=[(0, 1), (1, 2), (3, 0, "pending", False)])
    result = service(state, monkeypatch).compare(space_id, {**REQUEST, "horizon_days": 4})
    for scenario in result["scenarios"]:
        positions = {t["topic_id"]: i for i, t in enumerate(scenario["schedule"])}
        assert positions[topics[0]["id"]] < positions[topics[1]["id"]] < positions[topics[2]["id"]]
        first = next(t for t in scenario["schedule"] if t["topic_id"] == topics[0]["id"])
        assert first["prerequisite_ids"] == []


def test_scope_never_expands_to_missing_or_excluded_prerequisite(backend, monkeypatch):
    state, space_id, topics = setup(backend, count=3, edges=[(0, 1), (1, 2)])
    state.set_scope(space_id, {"topic_ids": [topics[1]["id"], topics[2]["id"]],
        "excluded_topic_ids": [topics[0]["id"]], "include_prerequisites": False, "expected_version": 2})
    result = service(state, monkeypatch).compare(space_id, REQUEST)
    for scenario in result["scenarios"]:
        assert scenario["schedule"] == []
        blocked = {item["topic_id"]: item for item in scenario["blocked"]}
        assert "excluded_prerequisite" in blocked[topics[1]["id"]]["reason_codes"]
        assert topics[0]["id"] in blocked[topics[1]["id"]]["prerequisite_ids"]
        assert "blocked_prerequisite" in blocked[topics[2]["id"]]["reason_codes"]


def test_calendar_week_caps_and_inclusive_profile_deadline(backend, monkeypatch):
    state, space_id, _ = setup(backend, count=12, weekly=30, target="2026-09-29")
    result = service(state, monkeypatch).compare(space_id, {**REQUEST, "horizon_days": 14})
    assert result["snapshot"]["effective_horizon_days"] == 4
    for scenario in result["scenarios"]:
        days, weeks = defaultdict(int), defaultdict(int)
        for task in scenario["schedule"]:
            day = date.fromisoformat(task["scheduled_date"])
            days[day] += task["estimated_minutes"]
            weeks[day - timedelta(days=day.weekday())] += task["estimated_minutes"]
            assert day <= date(2026, 9, 29)
        assert max(days.values()) <= scenario["minutes_per_day"]
        assert max(weeks.values()) <= 30
        assert sum(days.values()) == 60
        assert any("weekly_budget" in t["reason_codes"] for t in scenario["deferred"])


def test_past_deadline_returns_explicit_deferred_without_mutation(backend, monkeypatch):
    state, space_id, _ = setup(backend, target="2026-09-25")
    result = service(state, monkeypatch).compare(space_id, REQUEST)
    assert result["snapshot"]["effective_horizon_days"] == 0
    for scenario in result["scenarios"]:
        assert not scenario["schedule"]
        assert all("deadline" in row["reason_codes"] for row in scenario["deferred"])


def test_idempotent_snapshot_survives_restart_profile_changes_and_isolation(backend, monkeypatch):
    state, space_id, topics = setup(backend)
    comparison = service(state, monkeypatch).compare(space_id, REQUEST, "comparison-key")
    state.update_profile(space_id, {"weekly_minutes": 15, "expected_version": 1})
    restarted = LearningState(backend)
    assert service(restarted, monkeypatch).compare(space_id, REQUEST, "comparison-key") == comparison
    fresh = service(restarted, monkeypatch).compare(space_id, REQUEST, "new-key")
    assert fresh["snapshot"]["hash"] != comparison["snapshot"]["hash"]
    with pytest.raises(DomainConflict) as conflict:
        service(restarted, monkeypatch).compare(space_id, {**REQUEST, "horizon_days": 2}, "comparison-key")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    other, other_id, _ = setup(backend, count=2)
    with pytest.raises(DomainConflict):
        service(other, monkeypatch).compare(other_id, REQUEST, "comparison-key")
    with pytest.raises(DomainNotFound):
        service(other, monkeypatch).compare("missing-space", REQUEST)
    own = service(other, monkeypatch).compare(other_id, REQUEST)
    assert not {t["id"] for t in topics} & {t["topic_id"] for s in own["scenarios"] for t in s["schedule"]}


def test_cycles_are_explicitly_blocked_while_independent_subset_remains_feasible(backend, monkeypatch):
    state, space_id, topics = setup(backend, count=4, edges=[(0, 1), (1, 0), (1, 2)])
    result = service(state, monkeypatch).compare(space_id, REQUEST)
    for scenario in result["scenarios"]:
        assert [t["topic_id"] for t in scenario["schedule"]] == [topics[3]["id"]]
        blocked = {t["topic_id"]: t for t in scenario["blocked"]}
        assert "prerequisite_cycle" in blocked[topics[0]["id"]]["reason_codes"]
        assert "blocked_prerequisite" in blocked[topics[2]["id"]]["reason_codes"]


def test_strict_bounded_schema_and_large_graph(monkeypatch):
    from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
    implementation = module()
    from knowpath_backend.learning.plans.comparison_schemas import ComparePlans
    from pydantic import ValidationError
    for payload in [{"budgets_minutes_per_day": [20]}, {"budgets_minutes_per_day": [20, 20]},
        {"budgets_minutes_per_day": [True, 20]}, {"budgets_minutes_per_day": [20, "40"]},
        {"budgets_minutes_per_day": [20, 481]}, {**REQUEST, "horizon_days": 31}, {**REQUEST, "extra": 1}]:
        with pytest.raises(ValidationError):
            ComparePlans.model_validate(payload)
    state, space_id, _ = setup(InMemoryMaterialRepository(), count=implementation.MAX_TOPICS + 1)
    with pytest.raises(DomainConflict) as conflict:
        service(state, monkeypatch).compare(space_id, REQUEST)
    assert conflict.value.code == "COMPARISON_LIMIT_EXCEEDED"


def test_same_key_concurrent_memory_preview_is_identical(monkeypatch):
    from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
    state, space_id, _ = setup(InMemoryMaterialRepository())
    compare = service(state, monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: compare.compare(space_id, REQUEST, "concurrent-preview"), range(4)))
    assert all(item == results[0] for item in results)


def put_state(state, space_id, topic, *, status="mastered", stale=False):
    from knowpath_backend.learning.assessments.service import revision
    value = {"id": str(uuid4()), "space_id": space_id, "topic_id": topic["id"],
        "mastery_score": 0.95 if status == "mastered" else 0.3, "score_validity": "current",
        "topic_revision_id": "old-revision" if stale else revision(topic), "epoch": 0,
        "status": status, "evidence_ids": [], "independent_evidence_count": 3,
        "error_tags": [], "state_version": 1, "policy_version": "mastery-v1",
        "last_assessed_at": NOW, "next_review_at": None}
    with state.assessment_service.repository.transaction():
        state.assessment_service.repository.put_record("states", value)
    return value


def test_current_mastered_prerequisite_costs_nothing_but_stale_state_does(backend, monkeypatch):
    state, space_id, topics = setup(backend, count=2, edges=[(0, 1)])
    saved = put_state(state, space_id, topics[0])
    request = {"budgets_minutes_per_day": [10, 20], "horizon_days": 1}
    result = service(state, monkeypatch).compare(space_id, request)
    assert result["scenarios"][0]["summary"]["estimated_minutes"] == 10
    assert result["scenarios"][0]["already_satisfied_topic_ids"] == [topics[0]["id"]]
    assert [t["topic_id"] for t in result["scenarios"][0]["schedule"]] == [topics[1]["id"]]
    saved["topic_revision_id"] = "old-revision"
    with state.assessment_service.repository.transaction():
        state.assessment_service.repository.put_record("states", saved)
    stale = service(state, monkeypatch).compare(space_id, request)
    assert stale["scenarios"][0]["already_satisfied_topic_ids"] == []
    assert [t["topic_id"] for t in stale["scenarios"][0]["schedule"]] == [topics[0]["id"]]
    assert stale["scenarios"][0]["schedule"][0]["kind"] == "diagnostic"


def test_impossible_weak_goal_does_not_prevent_smaller_unknown_goal(backend, monkeypatch):
    state, space_id, topics = setup(backend, count=2)
    put_state(state, space_id, topics[0], status="unstable")
    result = service(state, monkeypatch).compare(space_id, {"budgets_minutes_per_day": [10, 15], "horizon_days": 1})
    low, high = result["scenarios"]
    assert low["schedule"][0]["topic_id"] == topics[1]["id"]
    assert "daily_budget" in low["deferred"][0]["reason_codes"]
    assert high["schedule"][0]["topic_id"] == topics[0]["id"]
    assert high["summary"]["weak_topics_covered"] == 1


def test_long_chain_is_bounded_iterative_and_never_schedules_past_budget(monkeypatch):
    from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
    count = 180
    state, space_id, topics = setup(InMemoryMaterialRepository(), count=count,
        edges=[(i, i + 1) for i in range(count - 1)], weekly=2400)
    result = service(state, monkeypatch).compare(space_id, {"budgets_minutes_per_day": [20, 40], "horizon_days": 2})
    for scenario, covered in zip(result["scenarios"], [4, 8]):
        assert [t["topic_id"] for t in scenario["schedule"]] == [t["id"] for t in topics[:covered]]
        assert len(scenario["deferred"]) == count - covered


def test_reset_invalidates_old_mastered_state_without_rewriting_it(backend, monkeypatch):
    state, space_id, topics = setup(backend, count=2, edges=[(0, 1)])
    put_state(state, space_id, topics[0])
    with state.assessment_service.repository.transaction():
        state.assessment_service.repository.put_record("resets", {"id": str(uuid4()), "space_id": space_id,
            "topic_ids": [topics[0]["id"]], "state_version": 2, "reason": "Test reset epoch", "created_at": NOW})
    before = business_snapshot(state)
    result = service(state, monkeypatch).compare(space_id, {"budgets_minutes_per_day": [10, 20], "horizon_days": 1})
    assert result["scenarios"][0]["already_satisfied_topic_ids"] == []
    assert result["scenarios"][0]["schedule"][0]["topic_id"] == topics[0]["id"]
    assert business_snapshot(state) == before


def test_http_contract_auth_idempotency_and_schema(monkeypatch):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.api.routers.plan_comparisons import router
    settings = LearningSettings(local_token="comparison-test-token")
    app = create_app(settings=settings)
    if "/api/v1/learning-spaces/{space_id}/plan-comparisons" not in app.openapi()["paths"]:
        app.include_router(router)
    state, space_id, _ = setup(app.state.learning_state.material_repository)
    service(state, monkeypatch)
    client = TestClient(app)
    url = f"/api/v1/learning-spaces/{space_id}/plan-comparisons"
    headers = {"X-Local-Token": "comparison-test-token", "Idempotency-Key": "http-preview"}
    assert client.post(url, json=REQUEST).status_code == 401
    assert client.post(url, json=REQUEST, headers={"X-Local-Token": "comparison-test-token"}).status_code == 400
    response = client.post(url, json=REQUEST, headers=headers)
    assert response.status_code == 200
    assert response.json()["scenarios"][0]["summary"]["selected_topic_count"] == 2
    assert client.post(url, json=REQUEST, headers=headers).json() == response.json()
    assert client.post(url, json={**REQUEST, "future_learning_gain": 0.9}, headers=headers).status_code == 422
    assert client.post(url + "?extra=1", json=REQUEST, headers=headers).status_code == 422


def test_existing_practice_and_task_rows_are_unchanged_by_preview(backend, monkeypatch):
    state, space_id, _ = setup(backend, count=4)
    plan = state.create_plan(space_id, {})
    state.update_task(plan["id"], plan["tasks"][0]["id"], {"status": "completed", "expected_plan_version": 1})
    before = business_snapshot(state)
    service(state, monkeypatch).compare(space_id, REQUEST, "existing-plan-preview")
    assert business_snapshot(state) == before


def test_missing_grounded_prerequisite_is_blocked_not_dropped(backend, monkeypatch):
    state, space_id, topics = setup(backend, count=2)
    binding = state.space_service.get(space_id)["bindings"][0]
    repository = state.graph_service.repository
    with repository.transaction():
        row = repository.get_record("graph_revisions", binding["graph_revision_id"])
        row["publication"]["snapshot"]["relations"].append({"id": "missing-parent",
            "type": "prerequisite_of", "from_id": "removed-topic", "to_id": topics[0]["id"],
            "status": "active", "confirmed": True, "source_refs": topics[0]["source_refs"]})
        repository.put_record("graph_revisions", row)
    result = service(state, monkeypatch).compare(space_id, REQUEST)
    for scenario in result["scenarios"]:
        assert scenario["blocked"][0]["prerequisite_ids"] == ["removed-topic"]
        assert "missing_prerequisite" in scenario["blocked"][0]["reason_codes"]
        assert [t["topic_id"] for t in scenario["schedule"]] == [topics[1]["id"]]


def test_malformed_pinned_graph_returns_safe_domain_failure(backend, monkeypatch):
    state, space_id, _ = setup(backend, count=2)
    binding = state.space_service.get(space_id)["bindings"][0]
    repository = state.graph_service.repository
    with repository.transaction():
        row = repository.get_record("graph_revisions", binding["graph_revision_id"])
        row["publication"]["snapshot"]["nodes"].append({"not_an_id": True})
        repository.put_record("graph_revisions", row)
    with pytest.raises(DomainConflict) as conflict:
        service(state, monkeypatch).compare(space_id, REQUEST)
    assert conflict.value.code == "INVALID_GRAPH_SNAPSHOT"


def test_same_key_concurrent_restart_instances_share_snapshot(backend, monkeypatch):
    state, space_id, _ = setup(backend, count=3)
    services = [service(LearningState(backend), monkeypatch) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda item: item.compare(space_id, REQUEST, "restart-concurrent"), services))
    assert all(item == results[0] for item in results)


def test_review_due_projection_uses_comparison_snapshot_clock(backend, monkeypatch):
    state, space_id, topics = setup(backend, count=2)
    put_state(state, space_id, topics[0])
    # Capture the projection boundary: comparisons must forward their frozen
    # clock rather than allowing a second wall-clock read to cross midnight.
    original = state.plan_sessions._planning_states
    moments = []
    def projection(identifier, *, as_of=None):
        moments.append(as_of)
        return original(identifier, as_of=as_of)
    monkeypatch.setattr(state.plan_sessions, "_planning_states", projection)
    service(state, monkeypatch).compare(space_id, REQUEST)
    assert moments == [NOW]


def test_upcoming_same_day_review_neither_spends_budget_nor_counts_as_due(backend, monkeypatch):
    import knowpath_backend.learning.assessments.service as assessment_module
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
    state, space_id, topics = setup(backend, count=2)
    class ReviewQuestions(FixedQuestions):
        def __init__(self, salt):
            self.salt = salt
        def generate(self, topics, payload):
            return [{**row, "prompt": row["prompt"] + self.salt}
                    for row in super().generate(topics, payload)]
    # Real distinct assessments/families and actual spaced application answers
    # satisfy the mastery gate. Two easy successes yield a three-day interval.
    for index, observed in enumerate(("2026-09-22T18:00:00+00:00", "2026-09-23T18:00:00+00:00")):
        monkeypatch.setattr(assessment_module, "now", lambda observed=observed: observed)
        state.assessment_service.generator = ReviewQuestions(str(index))
        created = state.create_assessment(space_id, {"question_count": 5, "topic_ids": [topics[0]["id"]]})
        assessment = state.get_assessment(created["id"])
        state.record_attempt(assessment["id"], {"answers": [
            {"question_id": question["id"], "expected_answer_revision": 0, "answer": "A"}
            for question in assessment["questions"]], "finalize": True})
    compare = service(state, monkeypatch)
    request = {"budgets_minutes_per_day": [10, 20], "horizon_days": 1}
    before = compare.compare(space_id, request)
    for scenario in before["scenarios"]:
        assert [task["topic_id"] for task in scenario["schedule"]] == [topics[1]["id"]]
        assert scenario["summary"]["estimated_minutes"] == 10
        assert scenario["summary"]["due_topics_covered"] == 0
        assert topics[0]["id"] in scenario["already_satisfied_topic_ids"]
    monkeypatch.setattr(module(), "now", lambda: "2026-09-26T18:00:00+00:00")
    reached = compare.compare(space_id, request)
    assert reached["scenarios"][0]["schedule"][0]["topic_id"] == topics[0]["id"]
    assert reached["scenarios"][0]["schedule"][0]["kind"] == "review"
    assert reached["scenarios"][0]["summary"]["due_topics_covered"] == 1
