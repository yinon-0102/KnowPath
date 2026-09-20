from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from knowpath_backend.learning.materials.service import MaterialVersion, SourceChunk
from knowpath_backend.learning.rag import chunking


def version(*texts, filename="sample.md", paths=None, pages=None):
    chunks = [SourceChunk(str(i), text, (paths or [()] * len(texts))[i], None, None,
                          (pages or [None] * len(texts))[i], sha256(text.encode()).hexdigest())
              for i, text in enumerate(texts)]
    return MaterialVersion("v1", "m1", filename, "text/plain", "a" * 64, 1,
                           "ready", datetime.now(timezone.utc), chunks)


def test_unclosed_atomic_content_is_preserved_but_marked_damaged():
    v = version('```python\nx = 1\n')
    rows = chunking.semantic_chunks(v, 'r', max_tokens=1000)
    assert rows[0]['source_text'].startswith('```')
    assert rows[0]['quality']['content_status'] == 'damaged'


def check_sources(v, chunks):
    originals = {c.id: c for c in v.chunks}
    covered = {c.id: [] for c in v.chunks}
    for c in chunks:
        parts = []
        for s in c["source_spans"]:
            original = originals[s["block"]]
            assert s["artifact_hash"] == original.content_hash
            assert s["material_version_id"] == v.id
            parts.append(original.text[s["start"]:s["end"]])
            covered[s["block"]].extend(range(s["start"], s["end"]))
        assert c["source_text"] == "\n".join(parts)
    for identifier, offsets in covered.items():
        assert len(offsets) == len(set(offsets)), "duplicate evidence"
        assert all(i in offsets for i, ch in enumerate(originals[identifier].text) if not ch.isspace())


def test_articles_split_inside_legacy_chunk_and_merge_across_pages():
    v = version("第一章 总则\n第一条 为了保护。\n第二条 未成年人的", "合法权益受保护。\n第三条 各方职责。",
                filename="law.pdf", pages=[1, 2])
    before = deepcopy(v)
    chunks = chunking.semantic_chunks(v, "r1", max_tokens=500, count_tokens=len)
    articles = [c for c in chunks if c["quality"]["unit_kind"] == "article"]
    assert len(articles) == 3
    assert "合法权益受保护。" in articles[1]["source_text"]
    assert {s["page"] for s in articles[1]["source_spans"]} == {1, 2}
    assert all(c["section_path"] == ["第一章 总则"] for c in articles)
    assert len({c["parent_id"] for c in articles}) == 1
    check_sources(v, chunks)
    assert v == before


def test_markdown_context_changes_and_code_math_across_old_blocks_stay_atomic():
    v = version("自然段一。\n\n自然段二。", "```python\nx = 1", "\ny = 2\n```", "$$\nA = B", "+ C\n$$", "另一概念。",
                paths=[("数学", "向量")] * 5 + [("数学", "矩阵")])
    chunks = chunking.semantic_chunks(v, "r1", max_tokens=500, count_tokens=len, document_title="课本")
    assert len(chunks) == 5
    assert [c["quality"]["unit_kind"] for c in chunks] == ["paragraph", "paragraph", "code", "math", "paragraph"]
    assert chunks[2]["source_text"].count("```") == 2
    assert len(chunks[2]["source_spans"]) == 2
    assert chunks[0]["parent_id"] != chunks[-1]["parent_id"]
    assert chunks[0]["retrieval_text"].startswith("课本 > 数学 > 向量\n")
    check_sources(v, chunks)


def test_analects_short_verses_are_meaningful_units_not_size_windows():
    v = version("学而第一", "1. 子曰：学而时习之。\n2. 有子曰：孝弟为本。", "为政第二\n1. 子曰：为政以德。", filename="lunyu.txt")
    chunks = chunking.semantic_chunks(v, "r1", max_tokens=500, count_tokens=len)
    verses = [c for c in chunks if c["quality"]["unit_kind"] == "verse"]
    assert len(verses) == 3
    assert verses[0]["section_path"] == ["学而第一"]
    assert verses[2]["section_path"] == ["为政第二"]
    check_sources(v, chunks)


def test_long_units_split_at_sentences_and_keep_continuation_and_ids():
    v = version("第一条 甲乙丙丁。戊己庚辛。", "壬癸子丑。第二句结束。", filename="law.pdf", pages=[1, 2])
    chunks = chunking.semantic_chunks(v, "r1", max_tokens=10, count_tokens=len)
    assert len(chunks) >= 3
    assert all(len(c["source_text"]) <= 10 for c in chunks)
    assert all(c["continuation_of"] == chunks[0]["chunk_id"] for c in chunks[1:])
    assert chunks == chunking.semantic_chunks(v, "r1", max_tokens=10, count_tokens=len)
    assert {c["chunk_id"] for c in chunks}.isdisjoint(c["chunk_id"] for c in chunking.semantic_chunks(v, "r2", max_tokens=10, count_tokens=len))
    check_sources(v, chunks)


@pytest.mark.parametrize("text", ["```python\n" + "a" * 50 + "\n```", "$$" + "a" * 50 + "$$", "文字 $" + "a" * 50 + "$ 末尾"])
def test_oversized_atomic_code_or_math_fails_explicitly(text):
    with pytest.raises(chunking.SemanticChunkingError, match="atomic"):
        chunking.semantic_chunks(version(text), "r1", max_tokens=20, count_tokens=len)


def test_unknown_structure_and_unbroken_text_never_drop_characters():
    v = version("abcdefghijklmnop", filename="plain.txt")
    chunks = chunking.semantic_chunks(v, "r1", max_tokens=5, count_tokens=len)
    assert all(c["parent_id"] is None and c["quality"]["structure"] == "unknown" for c in chunks)
    assert "".join(c["source_text"] for c in chunks) == v.chunks[0].text
    check_sources(v, chunks)


def test_ebook_end_marker_does_not_attach_license_to_last_verse():
    v = version("尧曰第二十\n1. 子曰：知命。", "*** END OF THE PROJECT GUTENBERG EBOOK 论语 ***", "License conditions.", filename="lunyu.txt")
    chunks = chunking.semantic_chunks(v, "r1", max_tokens=500, count_tokens=len)
    verses = [c for c in chunks if c["quality"]["unit_kind"] == "verse"]
    assert len(verses) == 1 and "GUTENBERG" not in verses[0]["source_text"]
    assert chunks[-1]["section_path"] == []
    check_sources(v, chunks)
