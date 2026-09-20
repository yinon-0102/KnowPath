"""Reproducible structural/mapping audit; not a human semantic-accuracy score.

Run: python -m knowpath_backend.rag_eval.inspect_chunks --sample-dir PATH --output PATH
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

from knowpath_backend.learning.materials.service import MaterialParser, MaterialVersion
from knowpath_backend.learning.rag.chunking import DEFAULT_TOKENIZER, semantic_chunks

SAMPLES = ("minors-protection-law-2024.pdf", "linear-algebra-zh.md", "lunyu-zh-hans.txt")


def inspect_sample(path: Path, *, max_tokens=1800):
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    extracted = MaterialParser().parse(raw, filename=path.name)
    # The old parser uses random IDs. Pin equivalent fixture artifacts to stable
    # IDs for this report only, without changing the production parser or data.
    old = [replace(c, id=sha256(f"{digest}:{i}:{c.content_hash}".encode()).hexdigest())
           for i, c in enumerate(extracted)]
    version = MaterialVersion(digest, digest, path.name, "application/octet-stream", digest,
                              len(raw), "ready", datetime(2000, 1, 1, tzinfo=timezone.utc), old)
    chunks = semantic_chunks(version, "sample-" + digest, max_tokens=max_tokens, document_title=path.stem)
    originals = {c.id: c for c in old}
    coverage = {c.id: Counter() for c in old}
    reconstruction_failures = []
    for chunk in chunks:
        parts = []
        for s in chunk["source_spans"]:
            original = originals[s["block"]]
            if s["artifact_hash"] != original.content_hash or not 0 <= s["start"] < s["end"] <= len(original.text):
                raise ValueError("invalid source mapping")
            parts.append(original.text[s["start"]:s["end"]])
            coverage[original.id].update(range(s["start"], s["end"]))
        if "\n".join(parts).encode() != chunk["source_text"].encode():
            reconstruction_failures.append(chunk["chunk_id"])
    missing = sum(1 for c in old for i, char in enumerate(c.text) if not char.isspace() and not coverage[c.id][i])
    duplicate = sum(1 for c in old for i, char in enumerate(c.text) if not char.isspace() and coverage[c.id][i] > 1)
    units = Counter(c["quality"]["unit_kind"] for c in chunks)
    article_pattern = r"^\s*(第\s*[〇零一二三四五六七八九十百千万两0-9]+\s*条)"
    articles = sorted({re.sub(r"\s", "", m[1]) for c in chunks
                       if c["quality"]["unit_kind"] == "article"
                       and (m := re.match(article_pattern, c["source_text"]))})
    paths = sorted({tuple(c["section_path"]) for c in chunks if c["section_path"]})
    result = dict(filename=path.name, raw_sha256=digest, legacy_artifacts=len(old), chunks=len(chunks),
                  unit_kinds=dict(units), section_paths=[list(p) for p in paths],
                  distinct_article_count=len(articles), distinct_article_ids=articles,
                  cross_page_chunks=sum(len({s["page"] for s in c["source_spans"]}) > 1 for c in chunks),
                  multi_artifact_chunks=sum(len(c["source_spans"]) > 1 for c in chunks),
                  continuation_chunks=sum(c["continuation_of"] is not None for c in chunks),
                  atomic_chunks=sum(c["quality"]["atomic"] for c in chunks),
                  short_verses_under_100_bytes=sum(c["quality"]["unit_kind"] == "verse" and c["quality"]["token_count"] < 100 for c in chunks),
                  maximum_source_token_count=max((c["quality"]["token_count"] for c in chunks), default=0),
                  exact_source_reconstruction=not reconstruction_failures,
                  missing_nonwhitespace_characters=missing, duplicated_nonwhitespace_characters=duplicate,
                  examples={kind: next(({"source_text": c["source_text"], "section_path": c["section_path"],
                                        "source_spans": c["source_spans"]} for c in chunks if c["quality"]["unit_kind"] == kind), None)
                            for kind in units})
    if reconstruction_failures or missing or duplicate:
        raise ValueError("source coverage audit failed")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=1800)
    args = parser.parse_args(argv)
    report = dict(profile={"tokenizer": DEFAULT_TOKENIZER, "max_tokens": args.max_tokens,
                           "budget_applies_to": "source_text; metadata prefix excluded"},
                  audit_scope="Immutable legacy extraction artifacts, not original binary file bytes. Whitespace joins follow CitationResolver.",
                  limitations=["Automated structural/mapping audit, not human semantic accuracy or answer quality.",
                               "Legacy parser already discarded Markdown headings and some blank lines; these cannot be recovered as evidence.",
                               "PDF running headers/page numbers are retained because removing them would discard source text.",
                               "Sample fixture identities are deterministic substitutes for parser-generated UUIDs; production identities are untouched."],
                  samples=[inspect_sample(args.sample_dir / name, max_tokens=args.max_tokens) for name in SAMPLES])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({s["filename"]: {key: s[key] for key in ("chunks", "unit_kinds", "distinct_article_count", "cross_page_chunks", "exact_source_reconstruction", "missing_nonwhitespace_characters")} for s in report["samples"]}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
