"""Build the real-material tree-RAG development set from v10 artifacts.

The builder is intentionally deterministic. It never invents source spans: each
gold span is copied from an existing v10 label or an exact adjacent chunk in the
frozen SQLite index.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
V10 = ROOT / ".rag-evaluation" / "v10"
OUT = ROOT / ".rag-evaluation" / "tree-v1"

BASE_IDS = (
    [f"real-law-{i:02d}" for i in range(1, 13)] + ["real-law-15", "real-law-16"]
    + [f"real-math-{i:02d}" for i in range(1, 13)] + ["real-math-14"]
    + [f"real-lunyu-{i:02d}" for i in (1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)]
)
SCENARIOS = ("tree_adjacent", "tree_condition", "tree_section", "tree_multievidence")


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _span_key(span):
    return tuple(span.get(key) for key in ("material_version_id", "artifact_hash", "start", "end", "page", "block"))


def _source_spans(chunk):
    return [dict(span) for span in chunk.get("source_spans", [])]


def _clean(text):
    return re.sub(r"\s+", " ", text).strip()


def _anchor(text):
    text = _clean(text)
    article = re.search(r"第[^。\n]{1,8}条", text)
    if article:
        return article.group(0)
    code = re.search(r"(?:shape|axis|transpose|sum|cumsum|dot|Frobenius|Hadamard)", text, re.I)
    if code:
        return code.group(0)
    words = re.findall(r"[\u4e00-\u9fff]{2,8}", text)
    return words[0] if words else text[:12]


def _question(base, neighbor, scenario):
    source = base["source_filename"]
    anchor = _anchor(neighbor["source_text"])
    if source == "minors-protection-law-2024.pdf":
        suffix = f"另外，请结合同一章节相邻内容中关于“{anchor}”的规定，说明它对前述问题的补充、条件或后续处置。"
    elif source == "linear-algebra-zh.md":
        suffix = f"另外，请结合同一小节相邻内容中关于“{anchor}”的说明，解释这一结论在相邻示例或定义中的具体表现。"
    else:
        suffix = f"另外，请结合同一篇章相邻章句中关于“{anchor}”的内容，概括它与前述主题的联系。"
    return f"{base['question']} {suffix}"


def _point(base, evidence_id, neighbor, scenario):
    anchor = _anchor(neighbor["source_text"])
    if base["source_filename"] == "minors-protection-law-2024.pdf":
        text = f"说明相邻内容中{anchor}所规定的补充条件、例外或后续处置。"
    elif base["source_filename"] == "linear-algebra-zh.md":
        text = f"说明相邻内容中{anchor}所展示的定义、运算或示例结论。"
    else:
        text = f"概括相邻章句中{anchor}表达的主要内容及其与问题主题的联系。"
    return {"point_id": evidence_id.replace("-e", "-p"), "text": text,
            "evidence_ids": [evidence_id], "required": True}


def build():
    base_rows = {row["question_id"]: row for row in _jsonl(V10 / "dataset.jsonl")}
    if len(BASE_IDS) != 40 or len(set(BASE_IDS)) != 40:
        raise AssertionError("tree dataset source selection must contain 40 unique rows")

    with sqlite3.connect(V10 / "evaluation.db") as connection:
        payloads = [json.loads(row[0]) for row in connection.execute("select payload from rag_chunks")]
    by_span = {}
    by_parent = defaultdict(list)
    for chunk in payloads:
        for span in _source_spans(chunk):
            by_span[_span_key(span)] = chunk
        by_parent[(chunk["retrieval_version_id"], chunk.get("parent_id"))].append(chunk)
    for chunks in by_parent.values():
        chunks.sort(key=lambda chunk: (chunk.get("ordinal", 0), chunk["chunk_id"]))

    result = []
    for number, qid in enumerate(BASE_IDS, 1):
        base = base_rows[qid]
        original_gold = [dict(span) for span in base.get("necessary_evidence", [])]
        if not original_gold:
            raise AssertionError(f"source row has no gold evidence: {qid}")
        base_chunk = by_span.get(_span_key(original_gold[0]))
        if base_chunk is None:
            raise AssertionError(f"cannot map source gold to chunk: {qid}")
        siblings = by_parent[(base_chunk["retrieval_version_id"], base_chunk.get("parent_id"))]
        index = next((i for i, chunk in enumerate(siblings) if chunk["chunk_id"] == base_chunk["chunk_id"]), None)
        if index is None:
            raise AssertionError(f"cannot locate base chunk in parent group: {qid}")
        existing_ids = {chunk["chunk_id"] for chunk in (by_span.get(_span_key(span)) for span in original_gold) if chunk}
        candidates = [siblings[j] for j in (index - 1, index + 1) if 0 <= j < len(siblings)]
        candidates = [chunk for chunk in candidates if chunk["chunk_id"] not in existing_ids and _source_spans(chunk)]
        if not candidates:
            raise AssertionError(f"no adjacent evidence candidate: {qid}")
        # Prefer the neighbor sharing the most non-trivial Chinese/Latin terms;
        # this makes B1's lexical adjacency guard applicable without adding a
        # relation absent from the indexed data.
        base_terms = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", base_chunk["source_text"].lower()))
        neighbor = max(candidates, key=lambda chunk: (
            len(base_terms.intersection(set(re.findall(r"[\w\u4e00-\u9fff]{2,}", chunk["source_text"].lower())))),
            -abs(chunk.get("ordinal", 0) - base_chunk.get("ordinal", 0)),
        ))
        neighbor_span = _source_spans(neighbor)[0]
        evidence = [dict(span) for span in original_gold]
        if _span_key(neighbor_span) not in {_span_key(span) for span in evidence}:
            evidence.append(neighbor_span)
        if len(evidence) < 2:
            raise AssertionError(f"tree item has fewer than two gold spans: {qid}")
        scenario = SCENARIOS[(number - 1) % len(SCENARIOS)]
        evidence_ids = [f"tree-v1-{number:02d}-e{i}" for i in range(1, len(evidence) + 1)]
        for span, evidence_id in zip(evidence, evidence_ids):
            span["evidence_id"] = evidence_id
        points = [dict(point) for point in base.get("required_answer_points", [])]
        for point in points:
            point["evidence_ids"] = [evidence_ids[min(i, len(evidence_ids) - 1)] for i, _ in enumerate(point.get("evidence_ids", [0]))]
        points.append(_point(base, evidence_ids[-1], neighbor, scenario))
        row = {
            "schema_version": 1, "question_id": f"tree-v1-{number:02d}",
            "family_id": f"tree-v1-family-{number:02d}", "source_kind": "real_material",
            "synthetic": False, "human_review_status": "pending",
            "source_filename": base["source_filename"], "raw_source_sha256": base["raw_source_sha256"],
            "question": _question(base, neighbor, scenario), "conversation_history": [],
            "categories": [scenario, "tree_required", "source_grounded"],
            "tree_scenario": scenario, "scope_snapshot_id": base["scope_snapshot_id"],
            "allowed_source_spans": base["allowed_source_spans"], "excluded_source_spans": [],
            "necessary_evidence": evidence, "context_evidence": [],
            "required_answer_points": points, "answerability": "answerable",
            "expected_answer_status": "answered", "missing_answer_points": [],
            "required_behavior": [], "forbidden_claims": [],
            "label_origin": "tree-specialized draft; requires independent human review",
        }
        result.append(row)

    if len(result) != 40 or len({row["question_id"] for row in result}) != 40:
        raise AssertionError("tree dataset must contain 40 unique rows")
    if {row["tree_scenario"] for row in result} != set(SCENARIOS):
        raise AssertionError("all four tree scenarios must be present")
    if any(len(row["necessary_evidence"]) < 2 for row in result):
        raise AssertionError("every tree question needs at least two gold evidence spans")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "dataset.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in result), encoding="utf-8")
    split = {"schema_version": 1, "dataset": "dataset.jsonl", "source_kind": "real_material",
             "human_review_status": "pending", "real_material_holdout_status": "not_run",
             "dev": {"family_ids": [row["family_id"] for row in result],
                     "question_ids": [row["question_id"] for row in result]},
             "heldout": {"family_ids": [], "question_ids": []}}
    (OUT / "split.json").write_text(json.dumps(split, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rubric = """# Tree RAG v1 evaluation rubric\n\nStatus: review pending. Every question requires at least two exact source spans.\nFull-task success requires every required answer point, admissible citations, and preserved conditions.\nHuman review is external and is not inferred from model self-checks or service success.\n"""
    (OUT / "rubric.md").write_text(rubric, encoding="utf-8")
    print(json.dumps({"questions": len(result), "scenarios": {name: sum(row["tree_scenario"] == name for row in result) for name in SCENARIOS},
                      "output": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    build()
