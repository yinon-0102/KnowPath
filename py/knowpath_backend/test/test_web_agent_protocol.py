"""Offline protocol baseline: real SDK frames and durable LearningState commands."""
import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from knowpath_backend.test.web_agent_support import (
    AgentHarness, FakeClock, FakeEmbedding, ScriptedChatClient,
    completion, make_protocol_agent,
)
from knowpath_backend.tools.base import Tool, ToolParameter


class DefinitionTool(Tool):
    def __init__(self):
        super().__init__("definition", "Read a synthetic course definition")
        self.calls = []

    def get_parameters(self):
        return [ToolParameter(name="topic", type="string", description="Topic", required=True)]

    def run(self, parameters):
        self.calls.append(parameters)
        return "Functions group reusable behavior."


def test_scripted_client_uses_sdk_types_and_copies_requests():
    response = completion(tool_calls=[("call_definition", "definition", {"topic": "functions"})])
    assert isinstance(response, ChatCompletion)
    client = ScriptedChatClient([response, completion("done")])
    messages = [{"role": "user", "content": "synthetic question"}]
    chunks = list(client.chat.completions.create(messages=messages, stream=True))
    messages[0]["content"] = "changed"
    assert all(isinstance(chunk, ChatCompletionChunk) for chunk in chunks)
    assert chunks[0].choices[0].delta.tool_calls[0].id == "call_definition"
    assert chunks[-2].choices[0].finish_reason == "tool_calls"
    assert chunks[-1].usage.total_tokens == 12
    assert client.requests[0]["messages"][0]["content"] == "synthetic question"
    assert client.chat.completions.create(messages=[], stream=False).choices[0].message.content == "done"
    with pytest.raises(AssertionError, match="exhausted"):
        client.chat.completions.create(messages=[])


def test_real_agent_executes_tool_loop_and_finalizes_memory(tmp_path):
    tool = DefinitionTool()
    client = ScriptedChatClient([
        completion(tool_calls=[("call_definition", "definition", {"topic": "functions"})]),
        completion("Functions group reusable behavior."),
    ])
    agent = make_protocol_agent(tmp_path / "agent", client, tools=[tool])
    try:
        assert agent.run("Explain functions") == "Functions group reusable behavior."
        assert tool.calls == [{"topic": "functions"}]
        assert len(client.requests) == 2
        assert all(request["stream"] is True for request in client.requests)
        second = client.requests[1]["messages"]
        call = next(message for message in second if message.get("tool_calls"))
        assert call["tool_calls"][0]["function"]["arguments"] == '{"topic": "functions"}'
        result = next(message for message in second if message["role"] == "tool")
        assert result["tool_call_id"] == "call_definition"
        assert "Functions group reusable behavior." in result["content"]
        assert [(message.role, message.content) for message in agent.get_history()] == [
            ("user", "Explain functions"), ("assistant", "Functions group reusable behavior.")]
    finally:
        agent.memory.close()


def test_fake_embedding_and_clock_are_deterministic():
    assert FakeEmbedding(dim=8).embed("synthetic functions") == FakeEmbedding(dim=8).embed("synthetic functions")
    clock = FakeClock()
    initial = clock.now()
    clock.advance(seconds=30)
    assert (clock.now() - initial).total_seconds() == 30
    assert clock.monotonic() == 30


def test_harness_restart_replays_sql_messages_and_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_RETRIEVAL_BACKEND", "keyword")
    monkeypatch.setenv("LEARNING_RAG_PLUGIN", "legacy")
    with AgentHarness(tmp_path) as harness:
        first = harness.send("Explain functions", idempotency_key="synthetic-turn")
        events = harness.state.events_for(first["run_id"])
        completed = next(event["data"] for event in events if event["event"] == "message.completed")
        assert {"message_id", "session_id", "text", "citations", "request_scope"} <= completed.keys()
        assert completed["text"] == "Functions group reusable behavior."
        assert completed["citations"][0]["material_id"] == harness.material_id
        names = [event["event"] for event in events]
        assert names.index("message.delta") < names.index("message.completed") < names.index("run.completed")
        cursor = int(next(event["id"] for event in events if event["event"] == "message.delta"))
        assert all(int(event["id"]) > cursor for event in harness.state.events_for(first["run_id"], after_id=cursor))
        harness.restart()
        assert harness.send("Explain functions", idempotency_key="synthetic-turn") == first
        assert harness.state.get_run(first["run_id"])["status"] == "succeeded"
        assert harness.engine.dialect.name == "sqlite"


def test_harness_commands_are_real_and_future_memory_operations_fail_explicitly(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_RETRIEVAL_BACKEND", "keyword")
    monkeypatch.setenv("LEARNING_RAG_PLUGIN", "legacy")
    with AgentHarness(tmp_path) as harness:
        pending = harness.send("Explain functions", dispatch=lambda *args: None)
        harness.cancel(pending["run_id"])
        assert harness.state.get_run(pending["run_id"])["status"] == "cancelling"
        record = harness.state.message_service.repository.records("messages", space_id=harness.space_id)[0]
        harness.state.message_service.generate(record["id"])
        assert harness.state.get_run(pending["run_id"])["status"] == "cancelled"
        before = harness.state.get_space(harness.space_id)["scope_version"]
        harness.switch_scope([harness.topic_id])
        assert harness.state.get_space(harness.space_id)["scope_version"] == before + 1
        for operation, dependency in ((harness.memory_state, "Task 3"),
                                      (harness.expire_lease, "Task 5"),
                                      (harness.migrate, "Task 11")):
            with pytest.raises(NotImplementedError, match=dependency):
                operation()
        harness.delete_material()
        assert harness.state.get_space(harness.space_id)["bindings"] == []


def test_harness_does_not_read_ambient_model_or_retrieval_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_RAG_PLUGIN", "legacy")
    monkeypatch.setenv("LEARNING_RETRIEVAL_BACKEND", "qdrant")
    monkeypatch.setenv("LEARNING_CONFIG_FILE", str(tmp_path / "must-not-read.json"))
    with AgentHarness(tmp_path) as harness:
        result = harness.send("Explain functions")
        assert harness.state.get_run(result["run_id"])["status"] == "succeeded"


def test_harness_rejects_configured_online_pipeline_before_creating_database(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_RAG_PLUGIN", "b1")
    with pytest.raises(ValueError, match="isolated launcher"):
        AgentHarness(tmp_path)
    assert not (tmp_path / "learning.db").exists()
