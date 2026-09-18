"""Strict contracts for material metadata commands."""
from typing import Literal
from pydantic import StrictBool, model_validator
from .space_schemas import Contract, DeleteSpace, Name, Version


class UpdateMaterial(Contract):
    name: Name | None = None
    status: Literal["archived"] | None = None
    expected_version: Version

    @model_validator(mode="after")
    def changed_fields(self):
        fields = self.model_fields_set - {"expected_version"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("provide non-null name or archived status")
        return self


class DeleteMaterial(DeleteSpace):
    cascade: StrictBool = False
