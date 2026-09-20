"""B1 bounded same-scope structural expansion without recursive loading."""
import importlib

from knowpath_backend.test.test_rag_plugins import Dense, Embedder, chunk, request


def plugin(dense=None):
    return importlib.import_module("knowpath_backend.learning.rag.tree").TreePlugin(Embedder(), dense or Dense())


def test_tree_keeps_thirty_seeds_and_at_most_ten_extensions():
    seeds = [chunk(f"a{i:02}", parent=f"p{i:02}", requires=[f"z{i:02}"]) for i in range(30)]
    extensions = [chunk(f"z{i:02}", "condition detail", parent=f"p{i:02}") for i in range(30)]
    result = plugin(Dense([r["chunk_id"] for r in seeds])).retrieve(request(), seeds + extensions)
    assert len(result["candidates"]) == 40
    trace = result["trace"]
    assert len(trace["seeds"]) == 30
    assert len(trace["extensions"]) == 10
    assert {entry["reason"] for entry in trace["extensions"]} == {"requires"}
    assert {c["channel"] for c in result["candidates"][:30]} == {"hybrid"}
    assert {c["channel"] for c in result["candidates"][30:]} == {"tree"}


def test_short_extensions_fill_remaining_slots_from_base_without_duplicates():
    rows = [chunk(f"a{i:02}", parent=f"p{i:02}") for i in range(50)]
    result = plugin().retrieve(request(vector_candidates=50), rows)
    ids = [c["chunk_id"] for c in result["candidates"]]
    assert len(ids) == len(set(ids)) == 40
    assert len(result["trace"]["extensions"]) == 0
    assert len(result["trace"]["fill"]) == 10


def test_expansion_rejects_cross_version_parent_unknown_and_excluded_links():
    seed = chunk("a", parent="p", requires=["b", "c", "d", "e", "excluded"])
    rows = [seed, chunk("b", "condition", version="m2", parent="p"),
            chunk("c", "condition", parent="other"), chunk("d", "condition", parent=None),
            chunk("e", "condition", parent="p", retrieval_version_id="r2")]
    result = plugin(Dense(["a"])).retrieve(request(), rows)
    assert [c["chunk_id"] for c in result["candidates"]] == ["a"]
    assert len(result["trace"]["rejected"]) == 5


def test_per_parent_limit_three_and_no_recursive_expansion():
    rows = [chunk("a", parent="p", requires=["b", "c", "d", "e"]),
            chunk("b", "condition", parent="p", requires=["z"]),
            chunk("c", "condition", parent="p"), chunk("d", "condition", parent="p"),
            chunk("e", "condition", parent="p"), chunk("z", "condition", parent="p")]
    result = plugin(Dense(["a"])).retrieve(request(), rows)
    assert [c["chunk_id"] for c in result["candidates"]] == ["a", "b", "c", "d"]
    assert any(r["reason"] == "parent_limit" for r in result["trace"]["rejected"])
    assert "z" not in [c["chunk_id"] for c in result["candidates"]]


def test_hot_parent_does_not_reduce_seed_or_fill_budget():
    rows = [chunk(f'a{i:02}', parent='same') for i in range(40)]
    result = plugin().retrieve(request(vector_candidates=40), rows)
    assert len(result['candidates']) == 40
    assert len(result['trace']['seeds']) == 30
    assert len(result['trace']['fill']) == 10


def test_continuation_can_supply_condition_without_repeating_query():
    rows = [chunk("a", ordinal=0), chunk("b", "condition", ordinal=1, continuation_of="a")]
    result = plugin(Dense(["a"])).retrieve(request(), rows)
    assert [c["chunk_id"] for c in result["candidates"]] == ["a", "b"]
    assert result["trace"]["extensions"][0]["reason"] == "continuation_of"


def test_adjacency_requires_lexical_overlap_and_known_parent():
    rows = [chunk("a", ordinal=1), chunk("b", "unrelated", ordinal=0), chunk("c", "alpha nearby", ordinal=2)]
    result = plugin(Dense(["a"])).retrieve(request(keyword_candidates=1, vector_candidates=1), rows)
    assert [c["chunk_id"] for c in result["candidates"]] == ["a", "c"]
    assert result["trace"]["extensions"][0]["reason"] == "adjacent_lexical"
    unknown = [chunk("a", parent=None, ordinal=1), chunk("c", "alpha nearby", parent=None, ordinal=2)]
    assert len(plugin(Dense(["a"])).retrieve(request(keyword_candidates=1, vector_candidates=1), unknown)["candidates"]) == 1


def test_low_rerank_budget_remains_a_hard_total_cap():
    rows = [chunk("a", requires=["b"]), chunk("b", "condition")]
    result = plugin(Dense(["a"])).retrieve(request(rerank_candidates=1), rows)
    assert len(result["candidates"]) == 1
    assert result["trace"]["extensions"] == []


def test_shared_title_alone_does_not_justify_adjacent_source_expansion():
    rows = [chunk("a", ordinal=0), chunk("b", "unrelated", ordinal=1, retrieval_text="alpha chapter\nunrelated")]
    result = plugin(Dense(["a"])).retrieve(request(keyword_candidates=1, vector_candidates=1), rows)
    assert [c["chunk_id"] for c in result["candidates"]] == ["a"]
    assert any(row["reason"] == "no_query_overlap" for row in result["trace"]["rejected"])


def test_later_seed_requirement_outranks_early_adjacency_when_extension_budget_is_full():
    seeds = [chunk(f"a{i:02}", parent=f"p{i:02}", ordinal=i * 3) for i in range(30)]
    seeds[-1]["requires"] = ["z_required"]
    # Ten eligible neighbours of earlier seeds would exhaust the extension
    # budget if relationship priority were applied separately inside each seed.
    neighbours = [chunk(f"z_near{i:02}", "alpha nearby", parent=f"p{i:02}", ordinal=i * 3 + 1)
                  for i in range(10)]
    required = chunk("z_required", "mandatory condition", parent="p29", ordinal=88)
    result = plugin(Dense([row["chunk_id"] for row in seeds])).retrieve(request(), seeds + neighbours + [required])
    extensions = result["trace"]["extensions"]
    assert len(result["trace"]["seeds"]) == 30
    assert len(extensions) == 10
    assert extensions[0] == {"chunk_id": "z_required", "parent_id": "p29",
                             "seed_id": "a29", "reason": "requires"}
    assert [row["chunk_id"] for row in extensions[1:]] == [f"z_near{i:02}" for i in range(9)]
    assert any(row["chunk_id"] == "z_near09" and row["reason"] == "extension_budget"
               for row in result["trace"]["rejected"])
    assert len({row["chunk_id"] for row in result["candidates"]}) == 40
