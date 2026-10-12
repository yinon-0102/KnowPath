"""Compact wire provenance, whole-chunk budgeting, and real stage boundaries."""
import copy
import json

import httpx
import pytest

from knowpath_backend.learning.assessments import generation
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.workers.runs import RunService
from knowpath_backend.test.test_learning_question_generation import topics, payload, raw
from knowpath_backend.test.test_learning_state_persistence import workspace, FixedQuestions


def compact(topics, payload, budget=24000):
    builder = getattr(generation, "build_compact_request", None)
    assert callable(builder), "Compact request builder is missing"
    return builder(topics, payload, source_budget=budget)


def test_legacy_injected_subclass_override_retains_old_signature_and_stages(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    class LegacyOverride(generation.DashScopeQuestionGenerator):
        called = False
        def generate(self, topics, payload):
            self.called = True
            return FixedQuestions().generate(topics, payload)
    provider = LegacyOverride()
    state.assessment_service.generator = provider
    result = state.assessment_service.create(space_id, {
        "question_count": 5, "difficulty_mix": {"easy": 1, "medium": 0, "hard": 0}})
    assert provider.called
    assert state.assessment_service.get(result['id'])['status'] == 'ready'
    run = state.get_run(result['run_id'])
    assert run['status'] == 'succeeded'
    assert [event['data']['stage'] for event in run['events'] if event['event'] == 'assessment.stage'] == [
        'generating_questions', 'validating_questions', 'ready']


def passage(text, chunk):
    return {"text": text, "source_refs": [{"chunk_id": chunk}]}


def test_shared_passages_sent_once_and_short_handles_expand(topics, payload, raw):
    topics[0]["source_passages"] = [passage("Shared facts.", "a")]
    topics[0]["source_refs"] = [{"chunk_id": "a"}]
    other = copy.deepcopy(topics[0])
    other.update(id="long-other-topic-id", revision_id="rev-other")
    other["source_passages"] = [passage("Shared facts.", "b")]
    other["source_refs"] = [{"chunk_id": "b"}]
    topics.append(other)
    payload["topic_ids"].append(other["id"])
    request, pack = compact(topics, payload)
    assert [t["id"] for t in request["topics"]] == ["t1", "t2"]
    assert request["sources"] == [{"id": "s1", "text": "Shared facts."}]
    assert request["topics"][0]["source_ids"] == ["s1"]
    assert request["topics"][1]["source_ids"] == ["s1"]
    assert "source_refs" not in json.dumps(request)
    wire = copy.deepcopy(raw)
    for q in wire:
        q.update(topic_id="t2", source_ids=["s1"])
        q.pop("source_refs")
    expanded = generation.expand_compact_questions(wire, pack)
    assert expanded[0]["topic_id"] == other["id"]
    assert expanded[0]["source_refs"] == [{"chunk_id": "b"}]
    assert len(generation.validate_questions(expanded, topics, payload)) == 5
    assert wire[0]["topic_id"] == "t2"


@pytest.mark.parametrize("topic_id,source_ids", [("unknown", ["s1"]), ("t1", ["unknown"]), ("t1", ["s2"]), ("t1", ["s1", "s1"]), ("t1", [])])
def test_unknown_cross_topic_or_repeated_handles_reject(topics, payload, raw, topic_id, source_ids):
    other = copy.deepcopy(topics[0])
    other.update(id="other", source_text="Other facts.")
    other["source_refs"] = [{"chunk_id": "other"}]
    topics.append(other)
    payload["topic_ids"].append("other")
    _, pack = compact(topics, payload)
    wire = copy.deepcopy(raw)
    for q in wire:
        q.update(topic_id="t1", source_ids=["s1"])
        q.pop("source_refs")
    wire[0].update(topic_id=topic_id, source_ids=source_ids)
    with pytest.raises(generation.QuestionGenerationError):
        generation.expand_compact_questions(wire, pack)


def test_budget_fairness_whole_chunks_and_only_offered_references(topics, payload):
    first = topics[0]
    first["source_passages"] = [passage("a" * 6, "a"), passage("b" * 6, "b")]
    first["source_refs"] = [{"chunk_id": "a"}, {"chunk_id": "b"}]
    other = copy.deepcopy(first)
    other.update(id="other", source_passages=[passage("c" * 6, "c"), passage("d" * 6, "d")], source_refs=[{"chunk_id": "c"}, {"chunk_id": "d"}])
    topics.append(other)
    payload["topic_ids"].append("other")
    request, pack = compact(topics, payload, budget=12)
    assert request["sources"] == [{"id": "s1", "text": "a" * 6}, {"id": "s2", "text": "c" * 6}]
    assert all(t["source_ids"] for t in request["topics"])
    assert pack["topics"]["t1"]["sources"] == {"s1": [{"chunk_id": "a"}]}
    with pytest.raises(generation.QuestionGenerationError):
        compact(topics, payload, budget=5)


def test_provider_uses_compact_wire_and_validates_stage_before_quality_gate(monkeypatch, topics, payload, raw):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    stages = []
    def respond(request):
        assert stages == ["generating_questions"]
        wire_request = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert wire_request["topics"][0]["id"] == "t1"
        wire = copy.deepcopy(raw)
        for q in wire:
            q.update(topic_id="t1", source_ids=["s1"])
            q.pop("source_refs")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"questions": wire})}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        generator = generation.DashScopeQuestionGenerator(client=client)
        assert generator.generate(topics, payload, on_stage=stages.append) == raw
    assert stages == ["generating_questions", "validating_questions"]


def test_stage_event_survives_sql_and_cannot_update_terminal(workspace):
    factory, _, _, _ = workspace
    runs = factory().run_service
    run_id = runs.create("assessment_generation", status="running")["id"]
    runs.append_event(run_id, "assessment.stage", {"stage": "generating_questions"})
    assert runs.get(run_id)["assessment_stage"] == "generating_questions"
    assert factory().run_service.get(run_id)["assessment_stage"] == "generating_questions"
    runs.request_cancel(run_id)
    before = runs.get(run_id)
    with pytest.raises(DomainConflict):
        runs.append_event(run_id, "assessment.stage", {"stage": "ready"})
    assert runs.get(run_id) == before


def test_legacy_generator_stages_and_individual_frozen_passages(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    result = state.assessment_service.create(space_id, {"question_count": 5, "difficulty_mix": {"easy": 1, "medium": 0, "hard": 0}}, dispatch=lambda *args: None)
    frozen = state.assessment_service.repository.get_record("assessments", result["id"])
    topic = frozen["snapshot"]["topics"][0]
    assert topic.get("source_passages"), "Individual frozen passages are missing"
    assert "\n\n".join(p["text"] for p in topic["source_passages"]) == topic["source_text"]
    class Observed(FixedQuestions):
        def generate(self, topics, payload):
            assert state.run_service.get(result["run_id"])["assessment_stage"] == "generating_questions"
            return super().generate(topics, payload)
    state.assessment_service.generator = Observed()
    state.assessment_service.generate(result["id"])
    run = state.run_service.get(result["run_id"])
    assert run["assessment_stage"] == "ready"
    assert [e["data"]["stage"] for e in run["events"] if e["event"] == "assessment.stage"] == ["generating_questions", "validating_questions", "ready"]


def test_cancel_during_provider_does_not_publish_validation_or_ready(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    result = state.assessment_service.create(space_id, {"question_count": 5, "difficulty_mix": {"easy": 1, "medium": 0, "hard": 0}}, dispatch=lambda *args: None)
    class Cancelling(FixedQuestions):
        def generate(self, topics, payload):
            state.run_service.request_cancel(result["run_id"])
            return super().generate(topics, payload)
    state.assessment_service.generator = Cancelling()
    state.assessment_service.generate(result["id"])
    run = state.run_service.get(result["run_id"])
    assert run["status"] == "cancelled"
    assert [e["data"]["stage"] for e in run["events"] if e["event"] == "assessment.stage"] == ["generating_questions"]


def test_invalid_stage_is_rejected_without_mutation():
    runs = RunService()
    run = runs.create("assessment_generation", status="running")
    with pytest.raises(DomainConflict):
        runs.append_event(run["id"], "assessment.stage", {"stage": "imaginary"})
    assert runs.get(run["id"]) == run


def test_compact_prompt_requests_concise_wording_without_reducing_grade_criteria():
    prompt = generation.COMPACT_SYSTEM_PROMPT.lower()
    assert "concise" in prompt
    assert "scoring criteria" in prompt
    assert "question_count" in prompt and "difficulty_counts" in prompt


def test_old_execution_cannot_stage_or_publish_to_replaced_run(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    repo = state.assessment_service.repository
    result = state.assessment_service.create(space_id, {"question_count": 5, "difficulty_mix": {"easy": 1, "medium": 0, "hard": 0}}, dispatch=lambda *args: None)
    replacement = []
    class Replacing(FixedQuestions):
        def generate(self, topics, payload):
            with repo.transaction():
                current = repo.get_record("assessments", result["id"])
                replacement.append(state.run_service.create("assessment_generation", status="running")["id"])
                current["run_id"] = replacement[0]
                repo.put_record("assessments", current)
            return super().generate(topics, payload)
    state.assessment_service.generator = Replacing()
    state.assessment_service.generate(result["id"])
    current = repo.get_record("assessments", result["id"])
    assert current["status"] == "generating" and current["questions"] == []
    assert state.run_service.get(replacement[0])["assessment_stage"] is None
    old = state.run_service.get(result["run_id"])
    assert [e["data"]["stage"] for e in old["events"] if e["event"] == "assessment.stage"] == ["generating_questions"]


def test_lost_lease_discards_output_before_validation_stage(workspace):
    from knowpath_backend.learning.workers.model_tasks import LeaseLost
    factory, space_id, _, _ = workspace
    state = factory()
    result = state.assessment_service.create(space_id, {"question_count": 5, "difficulty_mix": {"easy": 1, "medium": 0, "hard": 0}}, dispatch=lambda *args: None)
    class LostAfterCall:
        lost = False
        def check(self):
            if self.lost:
                raise LeaseLost()
    job = LostAfterCall()
    class Losing(FixedQuestions):
        def generate(self, topics, payload):
            job.lost = True
            return super().generate(topics, payload)
    state.assessment_service.generator = Losing()
    with pytest.raises(LeaseLost):
        state.assessment_service.generate(result["id"], job=job)
    run = state.run_service.get(result["run_id"])
    assert run["status"] == "running" and run["assessment_stage"] == "generating_questions"
    assert state.assessment_service.get(result["id"])["questions"] == []


def test_budget_reserves_a_whole_passage_for_each_topic_before_extra_chunks(topics, payload):
    first = topics[0]
    first["source_passages"] = [passage("a" * 6, "a"), passage("b" * 2, "b")]
    first["source_refs"] = [{"chunk_id": "a"}, {"chunk_id": "b"}]
    other = copy.deepcopy(first)
    other.update(id="other", source_passages=[passage("c" * 8, "c")], source_refs=[{"chunk_id": "c"}])
    topics.append(other)
    payload["topic_ids"].append("other")
    request, _ = compact(topics, payload, budget=12)
    assert sum(len(s["text"]) for s in request["sources"]) <= 12
    assert all(t["source_ids"] for t in request["topics"])
    assert {s["text"] for s in request["sources"]} == {"bb", "cccccccc"}


def test_empty_retry_checks_stage_boundary_before_second_model_call(monkeypatch, topics, payload):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    stopped, calls = [], []
    class Stopped(RuntimeError):
        pass
    def boundary(stage):
        if stopped:
            raise Stopped()
    def respond(request):
        calls.append(request)
        stopped.append(True)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"questions":[]}'}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(Stopped):
            generation.DashScopeQuestionGenerator(client=client).generate(topics, payload, on_stage=boundary)
    assert len(calls) == 1
