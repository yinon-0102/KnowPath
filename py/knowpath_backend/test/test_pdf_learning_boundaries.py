"""PDF source units must follow the document, rather than physical pages."""
from knowpath_backend.learning.materials.service import MaterialParser
from knowpath_backend.learning.materials.pdf_structure import structured_pdf_chunks


def test_cross_page_article_keeps_one_learning_scope_and_exact_page_fragments():
    pages = ["中华人民共和国示例法\n第一章 总则\n第一条 第一项规定。\n第二条 跨页规定前半",
             "后半及其完整内容。\n第三条 下一项规定。\n第二章 保护\n第四条 新章规定。"]
    chunks = structured_pdf_chunks(pages, MaterialParser()._parse_text)
    assert chunks
    first = [c for c in chunks if "第一条" in c.text][0]
    continuation = [c for c in chunks if "后半" in c.text][0]
    assert first.section_path == continuation.section_path
    assert "第一章" in first.section_path[0]
    assert continuation.page == 2
    assert continuation.line_start == 1
    next_chapter = [c for c in chunks if "第四条" in c.text][0]
    assert next_chapter.section_path != first.section_path
    assert "第二章" in next_chapter.section_path[0]
    assert all("第四条" not in c.text for c in chunks if c.section_path == first.section_path)


def test_learning_units_are_bounded_without_cutting_articles():
    pages = ["第一章 总则\n" + "\n".join(f"第{n}条 条文内容。" for n in range(1, 15))]
    chunks = structured_pdf_chunks(pages, MaterialParser()._parse_text)
    paths = list(dict.fromkeys(c.section_path for c in chunks))
    assert len(paths) == 3
    assert "第1—6条" in paths[0][0]
    assert "第7—12条" in paths[1][0]
    assert "第13—14条" in paths[2][0]


def test_unstructured_pdf_retains_legacy_fallback():
    assert structured_pdf_chunks(["ordinary paragraph\nwith no headings"], MaterialParser()._parse_text) is None


def test_compact_chapter_titles_never_merge_articles_across_chapters():
    chunks = structured_pdf_chunks(['第一章总则\n第一条 内容。\n第二章保护\n第二条 内容。'], MaterialParser()._parse_text)
    first = next(c for c in chunks if '第一条' in c.text)
    second = next(c for c in chunks if '第二条' in c.text)
    assert first.section_path != second.section_path
    assert '第一章' in first.section_path[0]
    assert '第二章' in second.section_path[0]


def test_repeat_upload_upgrades_legacy_version_once_and_preserves_old_sources():
    from dataclasses import replace
    from knowpath_backend.learning.materials.service import MaterialService, InMemoryMaterialRepository
    text = '第一章 总则\n第一条 第一项。\n第二条 第二项。'
    class Legacy:
        def parse(self, content, filename):
            return [replace(c, page=1) for c in MaterialParser()._parse_text(text)]
    class Structured:
        def parse(self, content, filename):
            return structured_pdf_chunks([text], MaterialParser()._parse_text)
    repo = InMemoryMaterialRepository()
    service = MaterialService(repo, Legacy())
    old = service.create(filename='law.pdf', content=b'%PDF-source', idempotency_key='old')
    service.parser = Structured()
    new = service.create_version(material_id=old.material.id, filename='law.pdf', content=b'%PDF-source', idempotency_key='upgrade')
    assert new.version.id != old.version.id
    assert repo.get_version(old.version.id).chunks == old.version.chunks
    assert new.version.chunks[0].section_path
    replay = service.create_version(material_id=old.material.id, filename='law.pdf', content=b'%PDF-source', idempotency_key='repeat')
    assert replay.version.id == new.version.id
