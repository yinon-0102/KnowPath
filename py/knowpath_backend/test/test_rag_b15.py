"""Tests for the B1.5 section coarse-to-fine retrieval plugin."""
import importlib
import time

import pytest

from knowpath_backend.learning.rag.contracts import RetrievalBudget, RetrievalRequest
from knowpath_backend.learning.rag.retrieval import RetrievalError


def chunk(identifier, text="alpha", *, section=("S",), version="m1", parent="p1", ordinal=0, **extra):
    return {"chunk_id": identifier, "material_version_id": version, "retrieval_version_id": "r1",
            "source_text": text, "retrieval_text": text, "parent_id": parent, "ordinal": ordinal,
            "section_path": list(section),
            "quality": {"structure": "known" if section else "unknown"},
            "source_spans": [{"material_version_id": version, "artifact_hash": "a" * 64,
                              "start": 0, "end": len(text)}], **extra}


def request(query="alpha", **budget):
    return RetrievalRequest(query=query, original_query=query, scope_snapshot_id="scope",
                            manifest_ids=("manifest",), budget=RetrievalBudget(**budget),
                            deadline=time.monotonic() + 30)


class Embedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts, *, query=False):
        self.calls.append((list(texts), query))
        return [[1.0, 0.0, 0.0]]


class SectionDense:
    def __init__(self, global_order, local_orders):
        self.global_order = list(global_order)
        self.local_orders = {key: list(value) for key, value in local_orders.items()}
        self.calls = []

    def search(self, vector, chunks, limit):
        ids = [row["chunk_id"] for row in chunks]
        key = tuple(ids)
        self.calls.append((key, limit))
        order = self.global_order if len(self.calls) == 1 else self.local_orders.get(key, ids)
        return [(identifier, 1.0) for identifier in order if identifier in ids][:limit]


class FailingFocusedDense(SectionDense):
    def search(self, vector, chunks, limit):
        if self.calls:
            raise RetrievalError("VECTOR_UNAVAILABLE")
        return super().search(vector, chunks, limit)


def plugin(embedder=None, dense=None, **options):
    module = importlib.import_module("knowpath_backend.learning.rag.section")
    return module.SectionPlugin(embedder or Embedder(), dense or SectionDense([], {}), **options)


@pytest.mark.parametrize("option", [{"section_limit": 4}, {"children_per_section": 4}])
def test_section_navigation_limits_are_hard_capped(option):
    with pytest.raises(ValueError):
        plugin(**option)


def test_section_navigation_reuses_query_embedding_and_adds_local_child():
    rows = [
        chunk("a1", "alpha first", section=("Target",), ordinal=0),
        chunk("a2", "alpha second", section=("Target",), ordinal=1),
        # The child is intentionally absent from both global channels.  The
        # focused vector query is the only path that can recover it.
        chunk("a3", "delta third", section=("Target",), ordinal=2),
        chunk("b1", "alpha other", section=("Other",), ordinal=0),
    ]
    embedder = Embedder()
    dense = SectionDense(["a1", "a2", "b1"], {tuple(r["chunk_id"] for r in rows[:3]): ["a3", "a2", "a1"]})
    result = plugin(embedder, dense).retrieve(request(), rows)

    ids = [candidate["chunk_id"] for candidate in result["candidates"]]
    assert "a3" in ids
    assert "a3" not in set(result["trace"]["seeds"])
    assert embedder.calls == [(["alpha"], True)]
    assert len(dense.calls) == 2
    assert set(dense.calls[1][0]) == {"a1", "a2", "a3"}
    assert result["trace"]["fallback"] is False
    assert result["trace"]["selected_sections"] == [
        {"retrieval_version_id": "r1", "material_version_id": "m1", "section_path": ["Target"]}
    ]
    assert result["trace"]["seeds"] == ["a1", "a2", "b1"]
    assert [entry["chunk_id"] for entry in result["trace"]["extensions"]] == ["a3"]


def test_section_navigation_falls_back_to_ordinary_without_consensus():
    rows = [
        chunk("a1", section=("A",)),
        chunk("b1", section=("B",)),
        chunk("c1", section=()),
    ]
    embedder = Embedder()
    dense = SectionDense(["a1", "b1", "c1"], {})
    result = plugin(embedder, dense).retrieve(request(), rows)

    assert result["trace"]["fallback"] is True
    assert result["trace"]["fallback_reason"] == "no_confident_section"
    assert len(dense.calls) == 1
    assert [candidate["chunk_id"] for candidate in result["candidates"]] == ["a1", "b1", "c1"]


def test_section_navigation_keeps_rerank_budget_and_never_mixes_versions():
    rows = [
        chunk("a1", section=("A",), version="m1"),
        chunk("a2", section=("A",), version="m1"),
        chunk("a3", section=("A",), version="m2"),
        chunk("b1", section=("B",), version="m1"),
    ]
    dense = SectionDense(["a1", "a2", "a3", "b1"], {tuple(r["chunk_id"] for r in rows[:2]): ["a2", "a1"]})
    result = plugin(Embedder(), dense).retrieve(request(rerank_candidates=2), rows)

    candidates = result["candidates"]
    assert len(candidates) == 2
    assert len({candidate["chunk_id"] for candidate in candidates}) == 2
    assert all(candidate["material_version_id"] == "m1" for candidate in candidates)
    assert [candidate["rank"] for candidate in candidates] == [1, 2]


def test_section_navigation_focus_query_is_version_isolated():
    rows = [
        chunk("m1-a1", section=("A",), version="m1", ordinal=0),
        chunk("m1-a2", section=("A",), version="m1", ordinal=1),
        chunk("m1-a3", "delta", section=("A",), version="m1", ordinal=2),
        chunk("m2-a1", "other unrelated", section=("A",), version="m2", ordinal=0),
        chunk("m2-a2", "other unrelated", section=("A",), version="m2", ordinal=1),
    ]
    dense = SectionDense(
        ["m1-a1", "m1-a2"],
        {tuple(row["chunk_id"] for row in rows[:3]): ["m1-a3", "m1-a2", "m1-a1"]},
    )
    result = plugin(Embedder(), dense).retrieve(request(), rows)
    assert all(candidate["material_version_id"] == "m1" for candidate in result["candidates"])
    assert set(dense.calls[1][0]) == {"m1-a1", "m1-a2", "m1-a3"}


def test_section_navigation_falls_back_to_global_results_when_focus_index_fails():
    rows = [chunk("a1", section=("A",)), chunk("a2", section=("A",)), chunk("b1", section=("B",))]
    dense = FailingFocusedDense(["a1", "a2", "b1"], {})
    result = plugin(Embedder(), dense).retrieve(request(), rows)

    assert result["trace"]["fallback"] is True
    assert result["trace"]["fallback_reason"] == "focused_retrieval_failed"
    assert [candidate["chunk_id"] for candidate in result["candidates"]] == ["a1", "a2", "b1"]
    assert result["trace"]["navigation_error"] == "service_unavailable"
    assert "VECTOR_UNAVAILABLE" not in str(result["trace"])


def test_section_navigation_does_not_swallow_scope_or_invalid_response_errors():
    class BrokenFocusedDense(SectionDense):
        def __init__(self, code):
            super().__init__(["a1", "a2"], {})
            self.code = code

        def search(self, vector, chunks, limit):
            if self.calls:
                raise RetrievalError(self.code)
            return super().search(vector, chunks, limit)

    rows = [chunk("a1", section=("A",)), chunk("a2", section=("A",))]
    for code in ("RETRIEVAL_SCOPE_INVALID", "RETRIEVAL_INVALID_RESPONSE", "VECTOR_PROFILE_MISMATCH"):
        try:
            plugin(Embedder(), BrokenFocusedDense(code)).retrieve(request(), rows)
        except RetrievalError as error:
            assert error.code == code
        else:
            raise AssertionError(f"{code} was swallowed")


def test_section_navigation_rejects_missing_or_unknown_structure_quality():
    rows = [chunk("a1", section=("A",)), chunk("a2", section=("A",))]
    rows[0].pop("quality")
    rows[1]["quality"] = {"structure": "unknown"}
    result = plugin(Embedder(), SectionDense(["a1", "a2"], {})).retrieve(request(), rows)
    assert result["trace"]["fallback_reason"] == "no_confident_section"


def test_section_navigation_limits_sections_and_focused_children():
    rows = []
    global_ids = []
    for section_index in range(4):
        path = (f"S{section_index}",)
        section_rows = [chunk(f"s{section_index}_{i}", section=path, ordinal=i)
                        for i in range(4)]
        rows.extend(section_rows)
        global_ids.extend(row["chunk_id"] for row in section_rows[:2])
    local_order = []
    for section_index in range(4):
        local_order.extend([f"s{section_index}_2", f"s{section_index}_3",
                            f"s{section_index}_0", f"s{section_index}_1"])
    dense = SectionDense(global_ids, {tuple(row["chunk_id"] for row in rows): local_order})
    result = plugin(Embedder(), dense).retrieve(request(), rows)
    selected = result["trace"]["selected_sections"]
    assert len(selected) <= 3
    extensions = result["trace"]["extensions"]
    assert len(extensions) <= 3 * len(selected)
    assert len({entry["chunk_id"] for entry in extensions}) == len(extensions)
    assert all(sum(entry["chunk_id"].startswith(f"s{index}_") for entry in extensions) <= 3
               for index in range(4))


def test_section_navigation_failure_returns_exact_flat_global_candidates():
    rows = [chunk("a1", section=("A",)), chunk("a2", section=("A",)), chunk("b1", section=("B",))]
    from knowpath_backend.learning.rag.plugins import OrdinaryPlugin
    expected = OrdinaryPlugin(Embedder(), SectionDense(["a1", "a2", "b1"], {})).retrieve(request(), rows)
    actual = plugin(Embedder(), FailingFocusedDense(["a1", "a2", "b1"], {})).retrieve(request(), rows)
    assert actual["candidates"] == expected["candidates"]


def test_section_navigation_propagates_focus_deadline():
    class DeadlineFocusedDense(SectionDense):
        def search(self, vector, chunks, limit):
            if self.calls:
                raise RetrievalError("RETRIEVAL_DEADLINE_EXCEEDED")
            return super().search(vector, chunks, limit)

    rows = [chunk("a1", section=("A",)), chunk("a2", section=("A",))]
    try:
        plugin(Embedder(), DeadlineFocusedDense(["a1", "a2"], {})).retrieve(request(), rows)
    except RetrievalError as error:
        assert error.code == "RETRIEVAL_DEADLINE_EXCEEDED"
    else:
        raise AssertionError("focus deadline was swallowed")
