import json
from collections import Counter
from pathlib import Path

import pytest

from knowpath_backend.rag_eval import prepare_real_dataset
from knowpath_backend.rag_eval.dataset import _source_paths, _validate_split, authorize_partition


ROOT = Path(__file__).resolve().parents[3] / "docs/research/tree-rag/evaluation/real-draft"


def test_real_drafts_have_sixty_questions_with_source_verified_labels():
    result = prepare_real_dataset.validate(ROOT)
    assert result["questions"] == 60 and result["documents"] == 3
    assert result["dev"] == 40 and result["heldout"] == 20
    assert result["verified_spans"] > 60


def test_drafts_do_not_claim_human_approval_or_synthetic_real_material():
    prepare_real_dataset.validate(ROOT)
    rows = [json.loads(line) for line in (ROOT / "dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 60
    assert sorted(Counter(r["source_filename"] for r in rows).values()) == [20, 20, 20]
    assert all(r["human_review_status"] == "pending" and r["synthetic"] is False for r in rows)
    assert {r["expected_answer_status"] for r in rows} == {"answered", "partial", "insufficient", "clarify"}
    split = json.loads((ROOT / "split.json").read_text(encoding="utf-8"))
    _validate_split(rows, split)
    for partition in ("dev", "heldout"):
        assert len({r["source_filename"] for r in rows if r["question_id"] in split[partition]["question_ids"]}) == 3
    with pytest.raises(ValueError, match="human review"):
        authorize_partition({"config": {"human_review_status": "pending"}}, rows, split, "heldout")


def test_every_reference_can_be_remapped_to_an_exact_legacy_artifact():
    prepare_real_dataset.validate(ROOT)
    rows = [json.loads(line) for line in (ROOT / "dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    for span in _source_paths(rows):
        assert span["raw_source_sha256"] and span["source_filename"]
        assert type(span["source_ordinal"]) is int and span["source_ordinal"] >= 0
        assert span["block"] and span["material_version_id"]
        assert span["artifact_path"] == f"sources/{span['raw_source_sha256']}-{span['source_ordinal']}.txt"


def test_negative_questions_have_real_context_without_fabricated_gold():
    prepare_real_dataset.validate(ROOT)
    rows = [json.loads(line) for line in (ROOT / "dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["expected_answer_status"] in {"insufficient", "clarify"}:
            assert row["necessary_evidence"] == [] and row["required_answer_points"] == []
            assert row["context_evidence"] and row["required_behavior"]
        if row["expected_answer_status"] == "partial":
            assert row["necessary_evidence"] and row["missing_answer_points"]
