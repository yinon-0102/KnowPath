"""Serializable request/candidate values; never retain clients or callbacks."""
from dataclasses import dataclass, field
from typing import Any, Protocol
from knowpath_backend.memory.contracts import MemoryMutation, VersionedContract


@dataclass(frozen=True)
class TurnBudgetLimits(VersionedContract):
    schema_version: int = 1
    max_model_calls: int = 8
    max_tool_rounds: int = 6
    max_input_tokens: int = 32768
    max_output_tokens: int = 4096
    deadline_at: str | None = None


@dataclass(frozen=True)
class TurnRequest(VersionedContract):
    message_id: str
    run_id: str
    space_id: str
    conversation_id: str
    input_text: str
    schema_version: int = 1
    answer_mode: str = 'auto'
    scope_fingerprint: str = ''
    context_fingerprint: str = ''
    context_epoch: int = 0
    space_sequence: int = 0
    material_bindings: list[dict[str, Any]] = field(default_factory=list)
    runtime_version: str = '1'
    policy_version: str = '1'
    budget: TurnBudgetLimits = field(default_factory=TurnBudgetLimits)

    @classmethod
    def from_dict(cls, value):
        result = super().from_dict(value)
        object.__setattr__(result, 'budget', TurnBudgetLimits.from_dict(result.budget))
        return result


@dataclass(frozen=True)
class TurnCandidate(VersionedContract):
    text: str
    schema_version: int = 1
    material_text: str = ''
    supplement_text: str = ''
    route: dict[str, Any] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    key_points: list[dict[str, Any]] = field(default_factory=list)
    citation_ids: list[str] = field(default_factory=list)
    delivered_sources: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    memory_mutation: MemoryMutation | None = None
    expected_memory_revision: int = 0

    @classmethod
    def from_dict(cls, value):
        result = super().from_dict(value)
        if result.memory_mutation is not None:
            object.__setattr__(result, 'memory_mutation', MemoryMutation.from_dict(result.memory_mutation))
        return result


class LearningAgentRuntime(Protocol):
    def run(self, request: TurnRequest, *, on_event=None, should_cancel=None) -> TurnCandidate: ...
