"""Explicit offline fixtures for Web Agent integration tests.

The protocol factory exercises today's real FunctionCallAgent, including its
stream parser and automatic finalizer. AgentHarness separately drives today's
LearningState/SQL message pipeline; it does NOT claim that pipeline uses Agent.
SQL L0-L5 inspection needs Tasks 3/7, memory-lease expiry Task 5, and migration
Task 11. These unavailable controls raise instead of manufacturing state.
All texts are synthetic. No developer database URL or credentials are read.
"""
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

from openai.types.chat import ChatCompletion, ChatCompletionChunk

from knowpath_backend.memory.embeddings import HashEmbedding


class FakeEmbedding(HashEmbedding):
    """Deterministic local test vectors; no semantic quality claim."""


class FakeClock:
    """Inject ``now``/``monotonic`` into consumers supporting clock injection."""
    def __init__(self, start=None):
        self.start = start or datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def now(self):
        return self.start + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def advance(self, *, seconds):
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        self.elapsed += seconds


def completion(text=None, *, tool_calls=(), finish_reason=None):
    """Build an SDK-validated synthetic response, including legal tool_calls."""
    calls = [{"id": identifier, "type": "function",
              "function": {"name": name, "arguments": json.dumps(arguments)}}
             for identifier, name, arguments in tool_calls]
    return ChatCompletion.model_validate({
        "id": "chatcmpl-synthetic", "object": "chat.completion", "created": 0,
        "model": "offline-scripted", "choices": [{"index": 0,
            "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
            "message": {"role": "assistant", "content": text, "tool_calls": calls or None}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    })


class ScriptedChatClient:
    """SDK-shaped chat client: immutable request snapshots, fail on extra calls.

    Nonstream requests return ChatCompletion. Stream requests return legal SDK
    chunks with fragmented tool arguments, finish reason, and a final usage frame.
    """
    def __init__(self, responses):
        self.responses = deque(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(deepcopy(request))
        if not self.responses:
            raise AssertionError("ScriptedChatClient responses exhausted")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, ChatCompletion):
            raise TypeError("Script entries must be ChatCompletion or Exception")
        if not request.get("stream", False):
            return response
        return iter(self._chunks(response))

    @staticmethod
    def _chunks(response):
        base = {"id": response.id, "object": "chat.completion.chunk",
                "created": response.created, "model": response.model}

        def frame(delta, finish=None):
            return ChatCompletionChunk.model_validate({**base, "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}]})

        choice = response.choices[0]
        message = choice.message
        if message.content:
            yield frame({"role": "assistant", "content": message.content})
        for index, call in enumerate(message.tool_calls or []):
            arguments = call.function.arguments
            split = max(1, len(arguments) // 2)
            yield frame({"role": "assistant", "tool_calls": [{"index": index,
                "id": call.id, "type": "function", "function": {
                    "name": call.function.name, "arguments": arguments[:split]}}]})
            yield frame({"tool_calls": [{"index": index,
                "function": {"arguments": arguments[split:]}}]})
        yield frame({}, choice.finish_reason)
        yield ChatCompletionChunk.model_validate({**base, "choices": [],
            "usage": response.usage.model_dump() if response.usage else None})


def make_protocol_agent(storage_dir, client, *, tools=()):
    """Real constructor, parser, tool loop, and finalizer; only provider is fake."""
    from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
    from knowpath_backend.memory import MemoryConfig
    from knowpath_backend.tools.registry import ToolRegistry

    storage_dir = Path(storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()
    for tool in tools:
        registry.register_tool(tool)
    llm = SimpleNamespace(provider="openai", model="offline-scripted",
                          client=client, temperature=0, max_tokens=256)
    return MyFunctionCallAgent(
        "offline-protocol", llm, registry, system_prompt="Synthetic course assistant.",
        memory_config=MemoryConfig(storage_dir=storage_dir, cold_backend="sqlite",
                                   vector_backend="memory", tick_mode="off",
                                   conflict_strength="off", context_relevance="bigram"),
        hooks=[], honesty_contract=False, max_steps=4,
    )


class SyntheticAnswer:
    """Legacy generator fixture. Kept explicit until Tasks 7-9 replace its path."""
    def classify(self, snapshot):
        return {"document_related": True, "educational": True, "topic_ids": [],
                "scope_uncertain": True, "reason": "Synthetic protocol fixture"}

    def generate(self, snapshot):
        return {"text": "Functions group reusable behavior.",
                "citation_ids": [snapshot["sources"][0]["chunk_id"]]}


class UnexpectedModel:
    """Fail closed if an unrelated business generator is accidentally invoked."""
    def generate(self, *args, **kwargs):
        raise AssertionError("Inject a dedicated offline fixture for this business generator")

    generate_json = generate


class AgentHarness:
    """Real, isolated SQL control surface; unsupported future controls are errors.

    Restart rebuilds LearningState over the same SQLite file. It deliberately
    does not fabricate checkpoint recovery or route legacy generation to Agent.
    Pass an explicit answer_generator to exercise a different local provider.
    The current LearningState constructor cannot explicitly disable its configured
    RAG factory. Non-legacy ambient configuration therefore fails closed; use the
    isolated launcher, which selects legacy until Tasks 7-9 change that boundary.
    """
    def __init__(self, root, *, answer_generator=None):
        from sqlalchemy import create_engine
        from knowpath_backend.learning.persistence.db import init_db

        self._check_pipeline_configuration()
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine("sqlite+pysqlite:///" + (root / "learning.db").as_posix())
        init_db(self.engine)
        self.answer_generator = answer_generator if answer_generator is not None else SyntheticAnswer()
        self.restart()
        material = self.state.material_service.create(
            filename="synthetic-functions.md", content=b"# Functions\n\nFunctions group reusable behavior.",
            idempotency_key="synthetic-material")
        self.material_id = material.material.id
        space = self.state.create_space({"name": "Synthetic course", "material_ids": [self.material_id]})
        self.space_id = space["id"]
        self.topic_id = self.state.topics_for_space(self.space_id)[0]["id"]

    def restart(self):
        from knowpath_backend.learning.config import LearningSettings
        from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
        from knowpath_backend.learning.rag.retrieval import KeywordRetriever
        from knowpath_backend.learning.state import LearningState

        self._check_pipeline_configuration()
        self.state = LearningState(SqlAlchemyMaterialRepository(self.engine),
                                   answer_generator=self.answer_generator,
                                   question_generator=UnexpectedModel(),
                                   note_generator=UnexpectedModel(), lesson_generator=UnexpectedModel(),
                                   source_retriever=KeywordRetriever(), context_settings=LearningSettings())
        return self.state

    @staticmethod
    def _check_pipeline_configuration():
        if os.getenv("LEARNING_RAG_PLUGIN", "legacy").strip().lower() != "legacy":
            raise ValueError("Use the isolated launcher: AgentHarness requires explicit legacy RAG configuration")

    def send(self, message, **kwargs):
        payload = message if isinstance(message, dict) else {"message": message}
        return self.state.send_message(self.space_id, payload, **kwargs)

    def cancel(self, run_id):
        return self.state.cancel_run(run_id)

    def delete_material(self, material_id=None, **payload):
        material_id = material_id or self.material_id
        material = self.state.material_repository.get_material(material_id)
        return self.state.delete_material(material_id, {
            "expected_version": material.version, "cascade": True, "confirm": True, **payload})

    def switch_scope(self, topic_ids):
        space = self.state.get_space(self.space_id)
        return self.state.set_scope(self.space_id, {
            "topic_ids": topic_ids, "expected_version": space["space_version"]})

    def memory_state(self):
        raise NotImplementedError("Task 3/7: SQL L0-L5 storage/runtime is not wired yet")

    def expire_lease(self):
        raise NotImplementedError("Task 5: memory-space lease does not exist yet; worker lease is distinct")

    def migrate(self):
        raise NotImplementedError("Task 11: message-to-memory backfill is not implemented yet")

    def close(self):
        self.engine.dispose()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
