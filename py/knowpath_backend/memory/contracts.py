"""Versioned JSON contracts for memory working copies and persistence adapters."""
from dataclasses import asdict, dataclass, field
import json
from typing import Any, ClassVar


class VersionedContract:
    VERSION: ClassVar[int] = 1

    def to_dict(self):
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @classmethod
    def from_dict(cls, value):
        payload = json.loads(json.dumps(value, allow_nan=False))
        if payload.get('schema_version') != cls.VERSION:
            raise ValueError(f'Unsupported {cls.__name__} schema version')
        return cls(**payload)

    def __post_init__(self):
        if self.schema_version != self.VERSION:
            raise ValueError(f'Unsupported {type(self).__name__} schema version')


@dataclass(frozen=True)
class MemoryCheckpoint(VersionedContract):
    schema_version: int = 1
    revision: int = 0
    space_id: str = ''
    conversation_id: str = ''
    context_epoch: int = 0
    context_fingerprint: str = ''
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryMutation(VersionedContract):
    schema_version: int = 1
    expected_revision: int = 0
    deletion_generation: int = 0
    checkpoint: MemoryCheckpoint | None = None
    operations: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value):
        result = super().from_dict(value)
        if isinstance(result.checkpoint, dict):
            object.__setattr__(result, 'checkpoint', MemoryCheckpoint.from_dict(result.checkpoint))
        return result
