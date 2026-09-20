"""Read semantic boundaries without altering the legacy extraction artifacts.

The virtual document adds only whitespace between old blocks. Every returned
span still addresses the original immutable block, never this virtual string.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from pathlib import PurePath
import re

from .scope import legacy_span


@dataclass(frozen=True)
class SemanticUnit:
    start: int
    end: int
    section_path: tuple[str, ...]
    kind: str = "paragraph"
    atomic: bool = False
    damaged: bool = False


class MappedDocument:
    def __init__(self, version):
        self.blocks = []
        pieces = []
        position = 0
        for chunk in version.chunks:
            span = legacy_span(version.id, chunk)
            if pieces:
                pieces.append("\n\n")
                position += 2
            self.blocks.append((position, position + len(chunk.text), chunk, span))
            pieces.append(chunk.text)
            position += len(chunk.text)
        self.text = "".join(pieces)
        self.starts = [b[0] for b in self.blocks]

    def context(self, position):
        index = bisect_right(self.starts, position) - 1
        return self.blocks[max(0, index)][2].section_path if self.blocks else ()

    def spans(self, start, end):
        index = max(0, bisect_right(self.starts, start) - 1)
        result = []
        for left, right, _, original in self.blocks[index:]:
            if left >= end:
                break
            a, b = max(start, left), min(end, right)
            if a < b:
                result.append(original.model_copy(update={"start": a - left, "end": b - left}))
        return result

    def source_text(self, start, end):
        index = max(0, bisect_right(self.starts, start) - 1)
        parts = []
        for left, right, chunk, _ in self.blocks[index:]:
            if left >= end:
                break
            a, b = max(start, left), min(end, right)
            if a < b:
                parts.append(chunk.text[a - left:b - left])
        return "\n".join(parts)


_NUMBER = r"[〇零一二三四五六七八九十百千万两0-9]+"
_ARTICLE = re.compile(r"^\s*第\s*" + _NUMBER + r"\s*条")
_LEGAL_HEADING = re.compile(r"^\s*第\s*" + _NUMBER + r"\s*([编章节])(?:\s+.*)?$")
_ANALECTS = re.compile(r"^\s*[\u3400-\u9fff]{1,12}第" + _NUMBER + r"\s*$")
_VERSE = re.compile(r"^\s*(?:\d+[.、]\s+|[【\[]\d+[.．:-]\d+[】\]])")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def semantic_units(document: MappedDocument, filename: str) -> list[SemanticUnit]:
    """Prefer article/verse/paragraph boundaries; fences and display math atomic."""
    markdown = PurePath(filename).suffix.lower() in {".md", ".markdown"}
    units = []
    path = ()
    old_context = None
    legal_levels = []
    start = None
    kind = "paragraph"
    atomic = False
    closing = None

    def flush(end):
        nonlocal start, kind, atomic
        if start is not None and document.text[start:end].strip():
            units.append(SemanticUnit(start, end, path, kind, atomic))
        start, kind, atomic = None, "paragraph", False

    offset = 0
    for line in document.text.splitlines(keepends=True):
        stripped = line.strip()
        end = offset + len(line)
        if closing is not None:
            closed = (bool(re.match(r"^\s*" + re.escape(closing[0]) + "{" + str(len(closing)) + r",}\s*$", stripped))
                      if kind == "code" else closing in stripped)
            if closed:
                flush(end)
                closing = None
            offset = end
            continue

        context = tuple(document.context(offset))
        if markdown and context != old_context:
            flush(offset)
            path = context
            old_context = context
        heading = _HEADING.match(line) if markdown else None
        legal = _LEGAL_HEADING.match(stripped) if not markdown else None
        analects = _ANALECTS.match(stripped) if not markdown else None
        if re.match(r"^\*{3}\s*(?:START|END) OF (?:THE|THIS) PROJECT GUTENBERG", stripped, re.I):
            flush(offset)
            path = ()
            units.append(SemanticUnit(offset, end, path, "document_boundary"))
        elif heading or legal or analects:
            flush(offset)
            if heading:
                path = path[:len(heading[1]) - 1] + (heading[2],)
            elif legal:
                level = "编章节".index(legal[1])
                legal_levels = [(n, value) for n, value in legal_levels if n < level]
                legal_levels.append((level, stripped))
                path = tuple(value for _, value in legal_levels)
            else:
                path = (stripped,)
            units.append(SemanticUnit(offset, end, path, "heading"))
        elif markdown and (_FENCE.match(line) or stripped.startswith(("$$", "\\["))):
            flush(offset)
            start, atomic = offset, True
            fence = _FENCE.match(line)
            kind = "code" if fence else "math"
            closing = fence[1] if fence else ("$$" if stripped.startswith("$$") else "\\]")
            if not fence and closing in stripped[2:]:
                flush(end)
                closing = None
        elif not markdown and (_ARTICLE.match(line) or _VERSE.match(line)):
            flush(offset)
            start = offset
            kind = "article" if _ARTICLE.match(line) else "verse"
        elif not stripped:
            if kind not in {"article", "verse"}:
                flush(offset)
        elif start is None:
            start = offset
        offset = end
    # Preserve damaged source for traceability, but it cannot support an answer.
    if closing is not None and start is not None:
        units.append(SemanticUnit(start, len(document.text), path, kind, atomic, True))
    else:
        flush(len(document.text))
    return units
