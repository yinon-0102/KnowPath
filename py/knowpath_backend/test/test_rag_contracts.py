"""Versioned RAG inputs must remain immutable and reject ambiguous identities."""
import pytest
from pydantic import ValidationError

from knowpath_backend.learning.rag.contracts import (
    Candidate, RetrievalBudget, RetrievalRequest, SourceSpan, Citation,
)


def span(**changes):
    return dict(material_version_id="v1", artifact_hash="a" * 64,
                start=0, end=8, page=1, block="p1", **changes)


@pytest.mark.parametrize("bounds", [(0, 0), (9, 8), (-1, 8)])
def test_source_span_rejects_empty_reversed_or_negative_intervals(bounds):
    payload = span()
    payload.update(start=bounds[0], end=bounds[1])
    with pytest.raises(ValidationError):
        SourceSpan(**payload)


def test_request_freezes_manifests_and_disallows_negative_budgets():
    manifests = ["m1"]
    request = RetrievalRequest(query="学校的义务？", original_query="它的义务？",
        scope_snapshot_id="scope1", manifest_ids=manifests,
        budget=RetrievalBudget(), deadline=100.0)
    manifests.append("m2")
    assert request.manifest_ids == ("m1",)
    with pytest.raises(ValidationError):
        request.scope_snapshot_id = "other"
    with pytest.raises(ValidationError):
        RetrievalBudget(rerank_candidates=-1)


def test_candidates_cannot_smuggle_text_or_nonfinite_score():
    payload = dict(chunk_id="c1", retrieval_version_id="r1",
                   material_version_id="v1", channel="vector", rank=1)
    with pytest.raises(ValidationError):
        Candidate(**payload, text="outside allowed scope")
    with pytest.raises(ValidationError):
        Candidate(**payload, score=float("nan"))


def test_v2_citation_requires_spans_from_its_own_original_version():
    payload = dict(material_id="m1", material_version_id="v2",
                   retrieval_version_id="r1", chunk_id="c1", source_spans=[span()])
    with pytest.raises(ValidationError):
        Citation(**payload)
    payload["material_version_id"] = "v1"
    citation = Citation(**payload)
    assert citation.citation_schema_version == 2
    assert citation.source_spans[0].page == 1
