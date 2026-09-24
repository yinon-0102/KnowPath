import copy
import itertools

import pytest

from knowpath_backend.learning.rag.context import assemble_context, preserve_baseline_with_anchors


def _row(identifier, *, group=None, requires=()):
    return dict(chunk_id=identifier, source_text="第74条" if identifier == "anchor" else identifier,
                evidence_group=group, requires=list(requires))


@pytest.mark.parametrize("question", ["第74条", "说明条件"])
@pytest.mark.parametrize("case,message", [
    ("duplicate_candidate", "duplicate context identity"),
    ("duplicate_baseline", "duplicate baseline identity"),
    ("missing_baseline", "baseline identity missing from candidates"),
])
def test_anchor_plan_rejects_invalid_identities_before_planning(question, case, message):
    from knowpath_backend.learning.rag.context import anchor_repacking_plan

    rows = [_row("primary"), _row("tail"), _row("anchor")]
    baseline = copy.deepcopy(rows[:2])
    if case == "duplicate_candidate":
        rows.append(dict(rows[0], source_text="conflicting source"))
    elif case == "duplicate_baseline":
        baseline.append(copy.deepcopy(baseline[0]))
    else:
        baseline[0] = _row("absent")
    original_rows, original_baseline = copy.deepcopy(rows), copy.deepcopy(baseline)
    with pytest.raises(ValueError, match=f"^{message}$"):
        anchor_repacking_plan(question, rows, baseline)
    assert rows == original_rows
    assert baseline == original_baseline


@pytest.mark.parametrize("rows,baseline_ids,expected", [
    ([_row("primary"), _row("d1", group="definition"), _row("d2", group="definition"), _row("anchor")],
     ["primary", "d1", "d2"], ["primary", "anchor", "d1", "d2"]),
    ([_row("a", requires=["b"]), _row("b"), _row("tail"), _row("anchor")],
     ["a", "b", "tail"], ["a", "b", "anchor", "tail"]),
    ([_row("b"), _row("a", requires=["b"]), _row("anchor")],
     ["b", "a"], ["b", "anchor", "a"]),
    ([_row("a", group="g", requires=["c"]), _row("b", group="g"), _row("c"), _row("tail"), _row("anchor")],
     ["a", "b", "c", "tail"], ["a", "b", "c", "anchor", "tail"]),
    ([_row("a", requires=["b"]), _row("b", requires=["a"]), _row("tail"), _row("anchor")],
     ["a", "b", "tail"], ["a", "b", "anchor", "tail"]),
    ([_row("primary"), _row("d1", group="g"), _row("between"), _row("d2", group="g"), _row("anchor")],
     ["primary", "d1", "between", "d2"], ["primary", "anchor", "d1", "between", "d2"]),
    ([_row("a", group="g"), _row("b", group="g"), _row("anchor")],
     ["a", "b"], ["a", "b", "anchor"]),
    ([_row("a", group="g"), _row("tail"), _row("outside", group="g"), _row("anchor")],
     ["a", "tail"], ["a", "tail", "outside", "anchor"]),
    ([_row("a"), _row("tail"), _row("anchor", requires=["missing"])],
     ["a", "tail"], ["a", "tail", "anchor"]),
])
def test_anchor_insertion_uses_largest_forward_dependency_closed_prefix(rows, baseline_ids, expected):
    original = copy.deepcopy(rows)
    baseline = [next(row for row in rows if row["chunk_id"] == identifier) for identifier in baseline_ids]
    result = preserve_baseline_with_anchors("结合第74条", rows, baseline)
    assert [row["chunk_id"] for row in result] == expected
    assert rows == original


def test_anchor_boundary_matches_finite_directed_graph_oracle():
    ids = ["a", "b", "c"]
    edges = [(left, right) for left in ids for right in ids if left != right]
    partitions = [(None, None, None), ("g", "g", None), ("g", None, "g"),
                  (None, "g", "g"), ("g", "g", "g")]
    for mask, groups, order in itertools.product(range(64), partitions, itertools.permutations(ids)):
        rows = [_row(identifier, group=groups[ids.index(identifier)],
                     requires=[right for bit, (left, right) in enumerate(edges)
                               if left == identifier and mask & (1 << bit)]) for identifier in order]
        rows.append(_row("anchor"))
        for baseline_size in (2, 3):
            baseline = rows[:baseline_size]
            boundaries = []
            for k in range(1, baseline_size):
                prefix = {row["chunk_id"] for row in baseline[:k]}
                if all(set(row["requires"]) <= prefix and all(
                        other["chunk_id"] in prefix for other in rows
                        if row["evidence_group"] and other["evidence_group"] == row["evidence_group"])
                       for row in baseline[:k]):
                    boundaries.append(k)
            k = max(boundaries, default=0)
            expected = rows if not k else baseline[:k] + [rows[-1]] + baseline[k:] + rows[baseline_size:-1]
            actual = preserve_baseline_with_anchors("第74条", rows, baseline)
            assert actual == expected, (mask, groups, order, baseline_size, k)


def test_explicit_article_anchor_is_prioritized_before_capacity_packing():
    from knowpath_backend.learning.rag.context import prioritize_explicit_anchors

    rows = [
        {"chunk_id": "a73", "source_text": "第七十三条 相邻条文。"},
        {"chunk_id": "a75", "source_text": "第七十五条 相邻条文。"},
        {"chunk_id": "a74", "source_text": "第七十四条 用户点名条文。"},
    ]
    ordered = prioritize_explicit_anchors("请说明第七十四条的实名和夜间条件", rows)
    assert [row["chunk_id"] for row in ordered] == ["a74", "a73", "a75"]


def test_missing_anchor_is_inserted_without_discarding_baseline_prefix():
    from knowpath_backend.learning.rag.context import preserve_baseline_with_anchors

    rows = [
        {"chunk_id": "a75", "source_text": "第七十五条 核心条件。"},
        {"chunk_id": "a73", "source_text": "第七十三条 相邻条文。"},
        {"chunk_id": "a74", "source_text": "第七十四条 点名补充。"},
    ]
    ordered = preserve_baseline_with_anchors("请结合第七十四条说明", rows, [rows[0], rows[1]])
    assert [row["chunk_id"] for row in ordered] == ["a75", "a74", "a73"]



def test_condition_group_is_all_or_nothing_under_budget():
    rows = [dict(chunk_id="conclusion", source_text="可申请", evidence_group="g"),
            dict(chunk_id="condition", source_text="仅限完成培训", evidence_group="g"),
            dict(chunk_id="short", source_text="定义", evidence_group="h")]
    assert [c["chunk_id"] for c in assemble_context(rows, max_tokens=7, count_tokens=len)] == ["short"]


def test_missing_required_evidence_excludes_dependent_conclusion():
    rows = [dict(chunk_id="conclusion", source_text="可申请", requires=["condition"]),
            dict(chunk_id="short", source_text="定义")]
    assert [c["chunk_id"] for c in assemble_context(rows, max_tokens=20, count_tokens=len)] == ["short"]


def test_dependencies_are_kept_even_if_reranker_orders_them_last():
    rows = [dict(chunk_id="conclusion", source_text="可申请", requires=["condition"]),
            dict(chunk_id="irrelevant", source_text="无关文字"),
            dict(chunk_id="condition", source_text="有条件")]
    assert {c["chunk_id"] for c in assemble_context(rows, max_tokens=7, count_tokens=len)} == {"conclusion", "condition"}
