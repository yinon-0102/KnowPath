"""Request-local, authoritative evidence for Agent retrieval tools."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .verification import VerificationError


@dataclass
class PreparedEvidence:
    status: str
    question: str
    sources: list[dict]
    trace: dict
    scope: Any
    deadline: float
    started: float


class EvidenceRegistry:
    """Keep server copies; model-visible IDs never grant arbitrary chunks."""

    def __init__(self):
        self._sources = {}

    def register(self, prepared: PreparedEvidence) -> list[dict]:
        rows = []
        for source in prepared.sources:
            identifier = f'E{len(self._sources) + 1}'
            self._sources[identifier] = copy.deepcopy(source)
            rows.append({**copy.deepcopy(source), 'evidence_id': identifier})
        return rows

    def resolve(self, identifiers) -> list[dict]:
        if (not isinstance(identifiers, (list, tuple))
                or any(not isinstance(identifier, str) or identifier not in self._sources
                       for identifier in identifiers)):
            raise VerificationError('RAG_SOURCE_INVALID')
        return [copy.deepcopy(self._sources[identifier]) for identifier in identifiers]
