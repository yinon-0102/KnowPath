"""Exact lexical BM25 and rank fusion checks; no embedding service needed."""
import hashlib
import importlib
import math

import pytest


def api():
    return importlib.import_module("knowpath_backend.learning.rag.bm25")


def chunk(identifier, text):
    return {"chunk_id": identifier, "retrieval_text": text}


def test_okapi_formula_and_length_normalization():
    index = api().BM25Index([chunk("a", "alpha alpha beta"), chunk("b", "alpha"), chunk("c", "beta")])
    scores = dict(index.search("alpha", {"a", "b", "c"}, 10))
    idf = math.log(1 + (3 - 2 + .5) / (2 + .5))
    assert scores["a"] == pytest.approx(idf * 2 * 2.5 / (2 + 1.5 * (.25 + .75 * 3 / (5 / 3))))
    assert scores["b"] == pytest.approx(idf * 2.5 / (1 + 1.5 * (.25 + .75 / (5 / 3))))
    assert "c" not in scores


def test_scope_changes_corpus_statistics_before_ranking():
    rows = [chunk("a", "alpha"), chunk("b", "alpha beta"), chunk("hidden", "alpha " * 100)]
    full = api().BM25Index(rows)
    scoped = api().BM25Index(rows[:2])
    assert full.search("alpha", {"a", "b"}, 10) == scoped.search("alpha", {"a", "b"}, 10)
    assert full.search("alpha", set(), 10) == []


def test_chinese_lexical_tokenizer_keeps_bigrams_latin_numbers_and_formula():
    m = api()
    tokens = m.tokenize("向量检索 BM25 E=mc^2 3.14")
    assert {"向", "量", "向量", "量检", "检索", "bm25", "e", "=", "mc", "^", "2", "3.14"} <= set(tokens)
    assert "exploratory" in m.TOKENIZER_VERSION
    assert m.BM25Index([chunk("a", "向量检索"), chunk("b", "经济学")]).search("向量", {"a", "b"}, 10)[0][0] == "a"


def test_bm25_receipt_checks_title_and_does_not_trust_mutated_input():
    row = chunk("a", "Title\nbody")
    index = api().BM25Index([row])
    expected = hashlib.sha256(row["retrieval_text"].encode()).hexdigest()
    assert index.verify([row])["chunks"] == [{"chunk_id": "a", "content_hash": expected}]
    row["retrieval_text"] = "Changed title\nbody"
    with pytest.raises(Exception, match="BM25_INDEX_NOT_READY"):
        index.verify([row])


@pytest.mark.parametrize("profile", [{"k1": float("nan")}, {"b": 2}, {"tokenizer": "unknown"}, {"unknown": 1}])
def test_bm25_profile_guard(profile):
    with pytest.raises(ValueError):
        api().BM25Index([], profile)


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5])
def test_bm25_limit_guard(limit):
    with pytest.raises(ValueError):
        api().BM25Index([]).search("x", set(), limit)


def test_rrf_uses_one_based_deduplicated_ranks_and_ignores_raw_score_magnitudes():
    rrf = importlib.import_module("knowpath_backend.learning.rag.fusion").reciprocal_rank_fusion
    result = rrf([[('a', -100), ('a', 1e100), ('b', 50)], [('b', -5), ('c', 0)]], 10)
    assert result[0][0] == "b"
    assert dict(result)["a"] == pytest.approx(1 / 61)
    assert dict(result)["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert rrf([["a"], ["b"]], 2) == [("a", 1 / 61), ("b", 1 / 61)]
    assert dict(rrf([["a"], ["b"]], 2, weights=[2, 1]))["a"] == pytest.approx(2 / 61)


@pytest.mark.parametrize("kwargs", [{"k": float("nan")}, {"k": -1}, {"weights": [-1]}, {"weights": [float("inf")]}, {"weights": []}, {"limit": 1001}])
def test_rrf_rejects_invalid_configuration(kwargs):
    rrf = importlib.import_module("knowpath_backend.learning.rag.fusion").reciprocal_rank_fusion
    with pytest.raises(ValueError):
        rrf([["a"]], **kwargs)
