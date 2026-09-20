"""Immutable contracts for the new RAG path; legacy chunk identities stay intact."""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegative = Annotated[int, Field(strict=True, ge=0)]
Positive = Annotated[int, Field(strict=True, gt=0)]


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class SourceSpan(FrozenContract):
    """Half-open Unicode character offsets within one immutable text artifact.

    Page and block disambiguate artifact-local coordinates. Offsets from a
    different extraction hash are never interchangeable, even for equal text.
    """
    material_version_id: Identifier
    artifact_hash: Digest
    start: NonNegative
    end: Positive
    page: Positive | None = None
    block: Identifier | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("source span must be a nonempty half-open interval")
        return self


class ScopeBinding(FrozenContract):
    material_id: Identifier
    material_version_id: Identifier
    graph_version: Positive


class ScopeSnapshot(FrozenContract):
    scope_snapshot_id: Identifier
    space_id: Identifier
    scope_version: NonNegative
    bindings: tuple[ScopeBinding, ...]
    allowed_spans: tuple[SourceSpan, ...]

    @model_validator(mode="after")
    def bound_versions(self):
        versions = [b.material_version_id for b in self.bindings]
        if len(set(versions)) != len(versions):
            raise ValueError("duplicate scope binding")
        if any(s.material_version_id not in versions for s in self.allowed_spans):
            raise ValueError("unbound source version")
        return self


class RetrievalBudget(FrozenContract):
    keyword_candidates: Positive = 30
    vector_candidates: Positive = 30
    rerank_candidates: Positive = 40
    context_tokens: Positive = 5000
    max_generation_calls: Positive = 2
    max_verification_calls: Positive = 2


class RetrievalRequest(FrozenContract):
    query: Annotated[str, Field(min_length=1)]
    original_query: Annotated[str, Field(min_length=1)]
    scope_snapshot_id: Identifier
    manifest_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    budget: RetrievalBudget
    deadline: Annotated[float, Field(gt=0)]  # monotonic clock deadline


class Candidate(FrozenContract):
    """Plugins return identities/ranks, never trusted source text."""
    chunk_id: Identifier
    retrieval_version_id: Identifier
    material_version_id: Identifier
    channel: Literal["bm25", "vector", "hybrid", "tree"]
    rank: Positive
    score: float | None = None
    parent_id: Identifier | None = None


class Citation(FrozenContract):
    citation_schema_version: Literal[2] = 2
    material_id: Identifier
    material_version_id: Identifier
    retrieval_version_id: Identifier
    chunk_id: Identifier
    source_spans: Annotated[tuple[SourceSpan, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def single_original(self):
        if any(s.material_version_id != self.material_version_id for s in self.source_spans):
            raise ValueError("citation spans must belong to its original version")
        return self


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    CLARIFY = "clarify"
    FAILED = "failed"


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNDETERMINED = "undetermined"


class ClaimCheck(FrozenContract):
    claim_id: Identifier
    status: SupportStatus
    source_spans: tuple[SourceSpan, ...] = ()
    reason: str
    missing_points: tuple[str, ...] = ()

    @model_validator(mode="after")
    def supported_has_evidence(self):
        if self.status == SupportStatus.SUPPORTED and not self.source_spans:
            raise ValueError("supported claims require original source spans")
        return self


class RetrievalResult(FrozenContract):
    candidates: tuple[Candidate, ...] = ()
    status: Literal["ready", "unavailable", "failed"]
    error_code: str | None = None
    trace_id: Identifier

    @model_validator(mode="after")
    def failure_is_explicit(self):
        if self.status != "ready" and (not self.error_code or self.candidates):
            raise ValueError("failed retrieval requires error and no publishable candidates")
        if self.status == "ready" and self.error_code:
            raise ValueError("ready result cannot have an error")
        return self


class RetrievalPlugin(Protocol):
    def prepare(self, snapshot: ScopeSnapshot, profile: dict, task_context: dict) -> dict: ...
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...
    def delete(self, identity: dict) -> dict: ...
    def close(self) -> None: ...
