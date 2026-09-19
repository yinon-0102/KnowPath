"""Strict contracts for material graph staging and publication."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from knowpath_backend.learning.spaces.schemas import Contract, Identifier

GraphVersion = Annotated[int, Field(strict=True, ge=0)]


class IngestMaterial(Contract):
    version_id: Identifier


class ReconcileGraph(Contract):
    version_id: Identifier
    expected_graph_version: GraphVersion


class ConflictResolution(Contract):
    conflict_id: Identifier
    action: Literal["keep_old", "use_new", "keep_both"]
    reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def reason_required(self):
        if self.action in {"keep_old", "use_new"} and not self.reason:
            raise ValueError("keep_old/use_new requires a reason")
        return self


class PublishGraph(Contract):
    expected_graph_version: GraphVersion
    resolutions: Annotated[list[ConflictResolution], Field(max_length=1000)] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_conflicts(self):
        ids = [item.conflict_id for item in self.resolutions]
        if len(ids) != len(set(ids)):
            raise ValueError("each conflict can be resolved only once")
        return self
