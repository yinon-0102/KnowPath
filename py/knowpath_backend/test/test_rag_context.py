from knowpath_backend.learning.rag.context import assemble_context


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
