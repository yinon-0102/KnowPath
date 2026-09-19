"""Source-backed correction commands with bounded, explicit replacement fields."""
from typing import Annotated, Any, Literal
from pydantic import Field, model_validator
from .space_schemas import Contract, Identifier
from .graph_schemas import GraphVersion

Reason = Annotated[str, Field(min_length=1, max_length=2000)]


class NodePatch(Contract):
    name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=10000)] | None = None


class RelationPatch(Contract):
    from_id: Identifier | None = None
    to_id: Identifier | None = None
    type: Literal['contains', 'prerequisite_of', 'related_to', 'assessed_by', 'explained_by', 'supersedes', 'contradicts'] | None = None


class CreateCorrection(Contract):
    kind: Literal['node', 'relation']
    target_id: Identifier
    action: Literal['reject', 'replace']
    reason: Reason
    source_ref: Identifier
    proposed_value: dict[str, Any] | None = None

    @model_validator(mode='after')
    def patch_contract(self):
        if self.action == 'replace':
            if not self.proposed_value:
                raise ValueError('replace requires a nonempty proposed_value')
            patch = (NodePatch if self.kind == 'node' else RelationPatch).model_validate(self.proposed_value)
            if any(value is None for value in self.proposed_value.values()):
                raise ValueError('replacement fields cannot be null')
            self.proposed_value = patch.model_dump(exclude_unset=True)
        elif self.proposed_value is not None:
            raise ValueError('reject does not accept proposed_value')
        return self


class ConfirmCorrection(Contract):
    expected_graph_version: GraphVersion
    reason: Reason
