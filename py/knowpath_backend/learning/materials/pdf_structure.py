"""Bounded legal learning units with immutable page/line source fragments."""
from dataclasses import replace
import re

NUMBER = r"[〇零一二三四五六七八九十百千万两0-9]+"
ARTICLE = re.compile(r"^\s*第\s*(" + NUMBER + r")\s*条")
HEADING = re.compile(r"^\s*第\s*" + NUMBER + r"\s*([编章节])[^\n]*$")
MAX_ARTICLES = 6
TARGET_CHARS = 1600


def needs_structural_upgrade(version):
    """A repeat upload must not reuse a known legacy page-only legal parse."""
    return (version.filename.lower().endswith('.pdf') and version.status == 'ready'
            and bool(version.chunks) and not any(c.section_path for c in version.chunks)
            and sum(bool(ARTICLE.match(line)) for c in version.chunks for line in c.text.splitlines()) >= 2)


def reusable_duplicate(repository, duplicate):
    if duplicate is None or not needs_structural_upgrade(duplicate[1]):
        return duplicate
    material, old = duplicate
    candidates = [v for v in repository.list_versions(material.id)
                  if v.content_hash == old.content_hash and v.filename.lower().endswith('.pdf')
                  and not needs_structural_upgrade(v)]
    return (material, max(candidates, key=lambda v: v.created_at)) if candidates else None


def structured_pdf_chunks(pages, parse_text):
    """Return None for unrecognized layouts, preserving the existing fallback.

    Page boundaries are locators, never semantic boundaries. Adjacent complete
    articles form a unit only inside the same chapter, up to six articles or
    1600 characters. A long article remains whole even above that target.
    """
    if sum(bool(ARTICLE.match(line)) for text in pages for line in text.splitlines()) < 2:
        return None
    units, lines, levels, chapter, label = [], [], [], "", None

    def flush():
        nonlocal lines
        if lines:
            units.append((chapter, label, lines))
            lines = []

    for page, text in enumerate(pages, 1):
        for number, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").splitlines(), 1):
            heading, article = HEADING.match(line.strip()), ARTICLE.match(line)
            if heading:
                flush()
                level = "编章节".index(heading[1])
                levels = [(n, title) for n, title in levels if n < level]
                levels.append((level, " ".join(line.split())))
                chapter = " / ".join(title for _, title in levels)
                label = None
            elif article:
                # Keep a chapter heading with its first article.
                if label is not None or (lines and not chapter):
                    flush()
                label = article[1]
            lines.append((page, number, line))
    flush()

    groups, current, size = [], [], 0
    for unit in units:
        length = sum(len(line) for _, _, line in unit[2])
        if current and (unit[0] != current[-1][0] or unit[1] is None or current[-1][1] is None
                        or len(current) >= MAX_ARTICLES or size + length > TARGET_CHARS):
            groups.append(current)
            current, size = [], 0
        current.append(unit)
        size += length
    if current:
        groups.append(current)

    chunks = []
    for group in groups:
        first, last = group[0][1], group[-1][1]
        scope = (f"第{first}条" if first == last else f"第{first}—{last}条") if first else "文档说明"
        # One flat learning topic avoids duplicating whole-chapter tasks and
        # their child tasks; the exact chapter remains in the scope label.
        path = ((group[0][0] + " · " if group[0][0] else "") + scope,)
        fragments = {}
        for _, _, source_lines in group:
            for page, number, line in source_lines:
                fragments.setdefault(page, []).append((number, line))
        for page, source_lines in fragments.items():
            text = "\n".join(line for _, line in source_lines)
            if not text.strip():
                continue
            # Reuse hashing/ID and paragraph construction; restore exact
            # source line numbers after extracting this page fragment.
            for chunk in parse_text(text):
                chunks.append(replace(chunk, section_path=path, page=page,
                    line_start=source_lines[chunk.line_start - 1][0],
                    line_end=source_lines[chunk.line_end - 1][0]))
    return chunks
