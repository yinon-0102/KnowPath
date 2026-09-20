"""Build or validate synthetic fixtures using only the Python standard library.

This is evaluation data tooling, not an online retrieval or scoring implementation.
Run with --check for read-only validation; without it, regenerate owned fixtures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = {
    "dev_permit": [
        "SYNTHETIC CONTROLLED SOURCE: Aurora laboratory handbook, page 1.\nThe standard equipment permit lasts 30 days.\n",
        "SYNTHETIC CONTROLLED SOURCE: Aurora laboratory handbook, page 2.\nFor visiting researchers, the permit lasts 7 days instead. All permit holders must complete safety training before use.\n",
    ],
    "dev_scope": [
        "SYNTHETIC CONTROLLED SOURCE: Boreal store, page 1.\nPublic guidance: refrigerated sample boxes must be stored at 4 degrees Celsius.\nRestricted appendix: the maintenance override code is COBALT-73.\n",
    ],
    "dev_conflict": [
        "SYNTHETIC CONTROLLED SOURCE: Cedar schedule, page 1.\nNotice A says the submission deadline is 12 October.\n",
        "SYNTHETIC CONTROLLED SOURCE: Cedar schedule, page 2.\nNotice B says the submission deadline is 15 October. Neither notice has precedence or a publication date.\n",
    ],
    "dev_absent": [
        "SYNTHETIC CONTROLLED SOURCE: Delta machine card, page 1.\nThe machine weighs 18 kilograms and requires a 220 volt supply. The card does not specify a purchase price.\n",
    ],
    "dev_ambiguous": [
        "SYNTHETIC CONTROLLED SOURCE: Elm procedures, page 1.\nThe registration procedure takes 2 working days. The calibration procedure takes 5 working days.\n",
    ],
    "heldout_exception": [
        "SYNTHETIC CONTROLLED SOURCE: Fjord shipment guide, page 1.\nStandard shipments leave on Monday. Fragile shipments always leave on Wednesday.\n",
        "SYNTHETIC CONTROLLED SOURCE: Fjord shipment guide, page 2.\nIf Monday is a local holiday, standard shipments leave on Tuesday instead.\n",
    ],
    "heldout_conflict": [
        "SYNTHETIC CONTROLLED SOURCE: Grove room cards, page 1.\nCard X lists room capacity as 24 people.\n",
        "SYNTHETIC CONTROLLED SOURCE: Grove room cards, page 2.\nCard Y lists room capacity as 28 people. The cards have no dates, approval marks, or precedence rule.\n",
    ],
    "heldout_absent": [
        "SYNTHETIC CONTROLLED SOURCE: Harbor pool note, page 1.\nThe pool opens at 08:00 and closes at 18:00. No water depth is stated in this note.\n",
    ],
}


def span(source: str, quote: str) -> dict:
    pages = SOURCES[source]
    content = "".join(pages)
    assert content.count(quote) == 1, (source, quote)
    start = content.index(quote)
    return {
        "material_id": f"controlled-{source}",
        "material_version_id": f"controlled-{source}-v1",
        "artifact_path": f"sources/{source}.txt",
        "artifact_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "start": start,
        "end": start + len(quote),
        "page": 1 + sum(start >= len("".join(pages[:i])) for i in range(1, len(pages))),
        "block": None,
        "quote": quote,
    }


def question(qid: str, source: str, query: str, category: list[str],
             facts: list[tuple[str, str]], answerability: str = "answerable",
             allowed: list[str] | None = None, expected: str = "answered",
             **extra: object) -> dict:
    evidence = [dict(evidence_id=f"{qid}-e{i}", **span(source, quote))
                for i, (quote, _) in enumerate(facts, 1)]
    return {
        "schema_version": 1,
        "question_id": qid,
        "family_id": source,
        "source_kind": "controlled",
        "synthetic": True,
        "human_review_status": "pending",
        "question": query,
        "conversation_history": [],
        "categories": category,
        "scope_snapshot_id": f"scope-{source}-v1",
        "allowed_source_spans": [span(source, q) for q in (allowed or SOURCES[source])],
        "excluded_source_spans": [],
        "necessary_evidence": evidence,
        "required_answer_points": [
            {"point_id": f"{qid}-p{i}", "text": answer,
             "evidence_ids": [evidence[i - 1]["evidence_id"]], "required": True}
            for i, (_, answer) in enumerate(facts, 1)
        ],
        "answerability": answerability,
        "expected_answer_status": expected,
        "required_behavior": [],
        "forbidden_claims": [],
        **extra,
    }


def expected_data() -> dict[str, bytes]:
    q = []
    q.append(question("ctrl-001", "dev_permit", "How long is a visiting researcher's permit valid, and what must happen before use?", ["condition_exception"], [
        ("For visiting researchers, the permit lasts 7 days instead.", "A visiting researcher's permit lasts 7 days, not the standard 30 days."),
        ("All permit holders must complete safety training before use.", "Safety training must be completed before use."),
    ], forbidden_claims=["A visiting researcher receives the standard 30-day permit."]))
    q.append(question("ctrl-002", "dev_permit", "Compare standard and visiting-researcher permits, including the shared prerequisite.", ["cross_page", "condition_exception"], [
        ("The standard equipment permit lasts 30 days.", "The standard duration is 30 days."),
        ("For visiting researchers, the permit lasts 7 days instead.", "The visiting-researcher exception is 7 days."),
        ("All permit holders must complete safety training before use.", "Both groups need safety training before use."),
    ]))
    public = "Public guidance: refrigerated sample boxes must be stored at 4 degrees Celsius."
    restricted = "Restricted appendix: the maintenance override code is COBALT-73."
    mixed = span("dev_scope", public + "\n" + restricted)
    q.append(question("ctrl-003", "dev_scope", "At what temperature must sample boxes be stored?", ["excluded_mixed_chunk", "scope_boundary"], [(public, "Refrigerated sample boxes must be stored at 4 degrees Celsius.")], allowed=[public], excluded_source_spans=[span("dev_scope", restricted)], controlled_candidates=[{"candidate_id": "mixed-public-restricted", "source_span": mixed, "expected_admission": "reject_whole_candidate"}, {"candidate_id": "public-only", "source_span": span("dev_scope", public), "expected_admission": "allow"}], required_behavior=["Reject the mixed candidate as a whole before generation; use the separately indexed allowed candidate."], forbidden_claims=["Any maintenance override code, including COBALT-73."]))
    q.append(question("ctrl-004", "dev_scope", "What is the maintenance override code?", ["excluded_mixed_chunk", "scope_boundary", "unanswerable"], [], answerability="unanswerable_in_scope", allowed=[public], expected="insufficient", excluded_source_spans=[span("dev_scope", restricted)], controlled_candidates=[{"candidate_id": "mixed-public-restricted", "source_span": mixed, "expected_admission": "reject_whole_candidate"}], required_behavior=["State that the permitted material does not support an answer; do not disclose the excluded code."], forbidden_claims=["Any maintenance override code, including COBALT-73."]))
    q.append(question("ctrl-005", "dev_conflict", "What is the submission deadline?", ["conflicting_evidence"], [
        ("Notice A says the submission deadline is 12 October.", "Notice A states 12 October."),
        ("Notice B says the submission deadline is 15 October.", "Notice B states 15 October."),
        ("Neither notice has precedence or a publication date.", "The material provides no basis to choose between the conflicting deadlines."),
    ], answerability="conflicting", expected="partial", required_behavior=["Explicitly preserve the unresolved conflict and decline to assert one authoritative deadline."], forbidden_claims=["One notice overrides the other."]))
    q.append(question("ctrl-006", "dev_absent", "What is the machine's purchase price?", ["absent_fact", "unanswerable"], [], answerability="unanswerable_in_scope", expected="insufficient", required_behavior=["Report that a purchase price cannot be determined from this complete permitted card."], forbidden_claims=["Any invented or externally sourced purchase price."]))
    q.append(question("ctrl-007", "dev_ambiguous", "How long does it take?", ["ambiguous_followup"], [], answerability="ambiguous", expected="clarify", conversation_history=[{"role": "user", "content": "I need to arrange registration and calibration."}], required_behavior=["Ask whether the user means registration or calibration before selecting a duration."], forbidden_claims=["Selecting a single procedure or duration without clarification."]))
    q.append(question("ctrl-008", "heldout_exception", "Monday is a local holiday. When do standard and fragile shipments leave?", ["cross_page", "condition_exception"], [
        ("If Monday is a local holiday, standard shipments leave on Tuesday instead.", "Standard shipments leave on Tuesday under the stated holiday condition."),
        ("Fragile shipments always leave on Wednesday.", "Fragile shipments leave on Wednesday."),
    ], forbidden_claims=["The standard shipment leaves on the holiday Monday."]))
    q.append(question("ctrl-009", "heldout_conflict", "What is the authoritative room capacity?", ["conflicting_evidence"], [
        ("Card X lists room capacity as 24 people.", "Card X states 24 people."),
        ("Card Y lists room capacity as 28 people.", "Card Y states 28 people."),
        ("The cards have no dates, approval marks, or precedence rule.", "An authoritative capacity cannot be selected from these conflicting cards."),
    ], answerability="conflicting", expected="partial", required_behavior=["Disclose the conflict and leave the authoritative capacity unresolved."], forbidden_claims=["Averaging the capacities or treating either card as authoritative."]))
    q.append(question("ctrl-010", "heldout_absent", "How deep is the pool?", ["absent_fact", "unanswerable"], [], answerability="unanswerable_in_scope", expected="insufficient", required_behavior=["Say the permitted pool note does not establish its depth."], forbidden_claims=["Any guessed or externally sourced water depth."]))
    split = {
        "schema_version": 1,
        "dataset": "dataset.jsonl",
        "source_kind": "controlled",
        "purpose": "synthetic_harness_validation_only",
        "human_review_status": "pending",
        "real_material_holdout_status": "pending_not_collected",
        "dev": {"family_ids": list(SOURCES)[:5], "question_ids": [r["question_id"] for r in q[:7]]},
        "heldout": {"family_ids": list(SOURCES)[5:], "question_ids": [r["question_id"] for r in q[7:]]},
    }
    faults = {
        "schema_version": 1, "source_kind": "controlled", "human_review_status": "pending",
        "purpose": "fault_injection_specifications_not_executed_results",
        "cases": [
            {"case_id": "fault-retrieval-timeout", "question_id": "ctrl-001", "stage": "retrieval", "fault": "timeout"},
            {"case_id": "fault-generation-unavailable", "question_id": "ctrl-003", "stage": "generation", "fault": "provider_unavailable"},
            {"case_id": "fault-verification-invalid", "question_id": "ctrl-008", "stage": "verification", "fault": "invalid_structured_response"},
        ],
        "expected": {"answer_status": "failed", "task_success": False, "include_in_all_request_denominator": True, "publish_unverified_answer": False, "preserve_paired_counterpart": True},
    }
    def encoded(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    files = {f"sources/{s}.txt": "".join(pages).encode("utf-8") for s, pages in SOURCES.items()}
    files["dataset.jsonl"] = ("\n".join(json.dumps(r, ensure_ascii=False) for r in q) + "\n").encode("utf-8")
    files["split.json"] = encoded(split)
    files["service_failure_fixtures.json"] = encoded(faults)
    return files


def validate() -> None:
    rows = [json.loads(line) for line in (ROOT / "dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    split = json.loads((ROOT / "split.json").read_text(encoding="utf-8"))
    ids = {r["question_id"] for r in rows}
    assert len(ids) == len(rows) == 10
    assert not set(split["dev"]["family_ids"]) & set(split["heldout"]["family_ids"])
    assert not set(split["dev"]["question_ids"]) & set(split["heldout"]["question_ids"])
    assert ids == set(split["dev"]["question_ids"]) | set(split["heldout"]["question_ids"])
    checked = 0
    def validate_span(s: dict) -> None:
        nonlocal checked
        raw = (ROOT / s["artifact_path"]).read_bytes()
        content = raw.decode("utf-8")
        assert hashlib.sha256(raw).hexdigest() == s["artifact_hash"]
        assert type(s["start"]) is int and type(s["end"]) is int
        assert 0 <= s["start"] < s["end"] <= len(content)
        assert content[s["start"]:s["end"]] == s["quote"]
        source = Path(s["artifact_path"]).stem
        assert s["material_id"] == f"controlled-{source}"
        assert s["material_version_id"] == f"controlled-{source}-v1"
        assert s["block"] is None
        pages = SOURCES[source]
        assert s["page"] == 1 + sum(s["start"] >= len("".join(pages[:i])) for i in range(1, len(pages)))
        checked += 1
    def contains(outer: dict, inner: dict) -> bool:
        return all(outer[k] == inner[k] for k in ("material_version_id", "artifact_hash")) and outer["start"] <= inner["start"] < inner["end"] <= outer["end"]
    for row in rows:
        assert row["source_kind"] == "controlled" and row["synthetic"] is True
        assert row["human_review_status"] == "pending"
        for name in ("dev", "heldout"):
            assert (row["question_id"] in split[name]["question_ids"]) == (row["family_id"] in split[name]["family_ids"])
        for span_list in ("allowed_source_spans", "excluded_source_spans", "necessary_evidence"):
            for s in row[span_list]:
                validate_span(s)
        for e in row["necessary_evidence"]:
            assert any(contains(a, e) for a in row["allowed_source_spans"])
        if "cross_page" in row["categories"]:
            assert len({e["page"] for e in row["necessary_evidence"]}) >= 2
        for a in row["allowed_source_spans"]:
            for e in row["excluded_source_spans"]:
                assert a["end"] <= e["start"] or e["end"] <= a["start"]
        evidence_ids = {e["evidence_id"] for e in row["necessary_evidence"]}
        for point in row["required_answer_points"]:
            assert point["required"] and point["text"] and point["evidence_ids"]
            assert set(point["evidence_ids"]) <= evidence_ids
        for candidate in row.get("controlled_candidates", []):
            s = candidate["source_span"]
            validate_span(s)
            permitted = any(contains(a, s) for a in row["allowed_source_spans"])
            assert permitted == (candidate["expected_admission"] == "allow")
        assert row["required_answer_points"] or row["required_behavior"]
    faults = json.loads((ROOT / "service_failure_fixtures.json").read_text(encoding="utf-8"))
    assert all(c["question_id"] in ids for c in faults["cases"])
    assert faults["expected"]["include_in_all_request_denominator"] is True
    print(f"Validated {len(rows)} synthetic questions, 8 isolated families, {checked} source spans, and {len(faults['cases'])} fault specifications.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate without writing files.")
    args = parser.parse_args()
    for relative, data in expected_data().items():
        path = ROOT / relative
        if args.check:
            assert path.read_bytes() == data, f"Fixture drift: {relative}; review source definitions before rebuilding."
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    validate()
