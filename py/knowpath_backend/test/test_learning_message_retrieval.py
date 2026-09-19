"""Retrieval runs after preparation and preserves message publication guards."""
import copy
import pytest
from knowpath_backend.learning.rag.retrieval import RetrievalError
from knowpath_backend.test.test_learning_state_persistence import workspace
from knowpath_backend.test.test_learning_messages import service


class Selection:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook
    def select(self, message, sources, *, limit=8):
        self.calls.append(copy.deepcopy(sources))
        if self.hook:
            self.hook()
        return sources[-1:]


def test_retrieval_selects_after_commit_and_persists_only_selected_refs(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    jobs = []
    retriever = Selection()
    state.message_service.retriever = retriever
    response = state.send_message(space_id, {"message": "Explain"}, idempotency_key="retrieval",
                                  dispatch=lambda fn, identifier: jobs.append((fn, identifier)))
    assert retriever.calls == [] and generator.calls == []
    assert not any(e["event"] == "tool.completed" for e in state.events_for(response["run_id"]))
    jobs[0][0](jobs[0][1])
    assert len(retriever.calls) == 1
    assert generator.calls[0]["sources"] == retriever.calls[0][-1:]
    assert all(row["graph_version"] == 1 for row in generator.calls[0]["sources"])
    events = factory().events_for(response["run_id"])
    tool = next(e["data"] for e in events if e["event"] == "tool.completed")
    assert [s["chunk_id"] for s in tool["source_refs"]] == [retriever.calls[0][-1]["chunk_id"]]
    assert all("text" not in ref for ref in tool["source_refs"])
    restored = factory().assessment_service.repository.get_record("messages", jobs[0][1])
    assert restored["snapshot"]["sources"] == generator.calls[0]["sources"]
    assert state.send_message(space_id, {"message": "Explain"}, idempotency_key="retrieval") == response
    assert len(retriever.calls) == 1


@pytest.mark.parametrize("code", ["VECTOR_INDEX_NOT_READY", "EMBEDDING_UNAVAILABLE", "VECTOR_UNAVAILABLE"])
def test_retrieval_failure_is_terminal_without_model_or_fake_answer(workspace, code):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    def fail():
        raise RetrievalError(code)
    state.message_service.retriever = Selection(fail)
    response = state.send_message(space_id, {"message": "Explain"})
    run = factory().get_run(response["run_id"])
    assert run["status"] == "failed" and run["error"]["code"] == code
    assert generator.calls == []
    assert not any(e["event"] in {"message.delta", "message.completed", "tool.completed"} for e in run["events"])


def test_retrieval_cannot_inject_foreign_or_modified_sources(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    retriever = Selection()
    retriever.select = lambda message, sources, **kwargs: [{**sources[0], "text": "Injected text"}]
    state.message_service.retriever = retriever
    response = state.send_message(space_id, {"message": "Explain"})
    run = factory().get_run(response["run_id"])
    assert run["status"] == "failed" and run["error"]["code"] == "RETRIEVAL_VALIDATION_FAILED"
    assert generator.calls == []


def test_cancel_during_retrieval_stops_before_answer_generation(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    jobs = []
    response = state.send_message(space_id, {"message": "Explain"}, dispatch=lambda fn, identifier: jobs.append((fn, identifier)))
    state.message_service.retriever = Selection(lambda: factory().cancel_run(response["run_id"]))
    jobs[0][0](jobs[0][1])
    assert factory().get_run(response["run_id"])["status"] == "cancelled"
    assert generator.calls == []


def test_scope_change_during_retrieval_stops_before_answer_generation(workspace):
    factory, space_id, topic_id, _ = workspace
    state, generator = service(factory)
    def change():
        other = factory()
        other.set_scope(space_id, {"topic_ids": [topic_id], "expected_version": other.get_space(space_id)["space_version"]})
    state.message_service.retriever = Selection(change)
    response = state.send_message(space_id, {"message": "Explain"})
    run = factory().get_run(response["run_id"])
    assert run["status"] == "failed" and run["error"]["code"] == "STALE_LEARNING_CONTEXT"
    assert generator.calls == []


def test_message_real_vector_retrieval_uses_pinned_candidates(workspace):
    from qdrant_client import QdrantClient
    from knowpath_backend.learning.rag.retrieval import VectorRetriever, QdrantVectorBackend
    from knowpath_backend.test.test_learning_vector_retrieval import Embedder
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    client = QdrantClient(":memory:")
    try:
        embedder = Embedder()
        retriever = VectorRetriever(embedder, QdrantVectorBackend(client, embedder.settings))
        candidates = state.message_service._sources(state.get_space(space_id))
        retriever.index(candidates)
        state.message_service.retriever = retriever
        response = state.send_message(space_id, {"message": "Explain reusable behavior"})
        assert factory().get_run(response["run_id"])["status"] == "succeeded"
        assert generator.calls and all(row in candidates for row in generator.calls[0]["sources"])
        assert embedder.calls[-1][1] is True
    finally:
        client.close()


def test_delete_during_retrieval_does_not_recreate_messages_or_call_answer_model(workspace):
    from knowpath_backend.learning.errors import DomainNotFound
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    def remove():
        other = factory()
        other.delete_space(space_id, {"confirm": True, "expected_version": other.get_space(space_id)["space_version"]})
    state.message_service.retriever = Selection(remove)
    response = state.send_message(space_id, {"message": "Explain"})
    assert generator.calls == []
    assert factory().assessment_service.repository.records("messages", space_id=space_id) == []
    with pytest.raises(DomainNotFound):
        factory().get_run(response["run_id"])


def test_api_closes_its_owned_retriever_on_shutdown(monkeypatch):
    import knowpath_backend.learning.api.application as module
    from fastapi.testclient import TestClient
    closed = []
    retriever = Selection()
    retriever.close = lambda: closed.append(True)
    monkeypatch.setattr(module, "configured_retriever", lambda settings: retriever)
    with TestClient(module.create_app()) as client:
        assert client.get("/api/v1/health").status_code == 200
    assert closed == [True]


def test_snapshot_keeps_more_than_eight_candidates_until_retrieval():
    from knowpath_backend.learning.state import LearningState
    from knowpath_backend.test.test_learning_messages import FixedAnswer
    state = LearningState(answer_generator=FixedAnswer(), source_retriever=Selection())
    content = "# Functions\n" + "\n\n".join("Paragraph " + str(i) for i in range(12))
    resource = state.material_service.create(filename="many.md", content=content.encode(), idempotency_key="many")
    space = state.create_space({"name": "Study", "material_ids": [resource.material.id]})
    topic = state.space_service.bound_topics(space)[0]
    state.set_scope(space["id"], {"expected_version": 1, "topic_ids": [topic["id"]]})
    state.send_message(space["id"], {"message": "Explain"})
    assert len(state.message_service.retriever.calls[0]) == 12
