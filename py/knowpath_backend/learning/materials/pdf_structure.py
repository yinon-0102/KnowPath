"""Bounded legal learning units with immutable page/line source fragments."""
from dataclasses import replace
from hashlib import sha256
import re
from uuid import uuid4

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


def graphics_coverage(operations):
    """Recognize a small rule-line whitelist; other painted vectors need review."""
    paint = {b'f', b'F', b'f*', b'S', b's', b'B', b'B*', b'b', b'b*'}
    path, stack, width, transformed, count = [], [], 1.0, False, 0
    present, review = False, False
    for arguments, operation in operations:
        if operation == b'q':
            stack.append((width, transformed))
        elif operation == b'Q' and stack:
            width, transformed = stack.pop()
        elif operation == b'w':
            width = float(arguments[0])
        elif operation == b'cm':
            # Translation preserves rule geometry. Scaling, shear and rotation
            # cannot be safely classified without rendering the complete page.
            transformed = transformed or tuple(float(v) for v in arguments[:4]) != (1.0, 0.0, 0.0, 1.0)
        elif operation in {b'm', b'l', b're', b'c', b'v', b'y', b'h'}:
            path.append((operation, [float(v) for v in arguments]))
        elif operation == b'n':
            path = []
        elif operation in paint:
            present, count = True, count + 1
            rule = False
            if not transformed and count <= 4:
                if operation == b'S' and len(path) == 2 and [row[0] for row in path] == [b'm', b'l']:
                    first, last = path[0][1], path[1][1]
                    rule = abs(first[1] - last[1]) < 0.01 and 0 < width <= 3
                elif len(path) == 1 and path[0][0] == b're':
                    side_a, side_b = abs(path[0][1][2]), abs(path[0][1][3])
                    short, long = min(side_a, side_b), max(side_a, side_b)
                    rule = 0 < short <= 3 and long >= short * 20 and (
                        operation in {b'f', b'F', b'f*'} or 0 < width <= 3)
            review = review or not rule
            path = []
        elif operation in {b'Do', b'sh', b'INLINE IMAGE'}:
            present, review = True, True
    return present, review


def legal_document(pages, structured, document):
    """Keep article scopes while classifying margin/front/reference sources.

    This adapter applies only to a new parse. Existing legal chunks and their
    legacy name identities never pass through it on reads or repeat uploads.
    """
    from .document_structure import ParsedDocument, signature
    manifest = document.manifest
    extra_units = [u for u in manifest['units'] if u['content_role'] in {'front_matter', 'footer', 'reference'}]
    extra_ids = {cid for u in extra_units for cid in u['chunk_ids']}
    extras = [c for c in document.chunks if c.id in extra_ids]
    excluded = {(c.page, line) for c in extras for line in range(c.line_start, c.line_end + 1)}
    original = [text.replace('\r\n', '\n').replace('\r', '\n').splitlines() for text in pages]
    filtered = []
    for chunk in structured:
        batch = []
        def flush():
            if batch:
                text = '\n'.join(line for _, line in batch)
                filtered.append(replace(chunk, id=str(uuid4()), text=text, line_start=batch[0][0],
                    line_end=batch[-1][0], content_hash=sha256(text.encode()).hexdigest()))
                batch.clear()
        for line in range(chunk.line_start, chunk.line_end + 1):
            if (chunk.page, line) in excluded:
                flush()
            else:
                batch.append((line, original[chunk.page - 1][line - 1]))
        flush()
    groups = {}
    for chunk in filtered:
        groups.setdefault(chunk.section_path, []).append(chunk)
    manifest['units'] = [{'key': 'legal:' + signature('/'.join(path)), 'parent_key': None,
        'name': path[-1], 'number': '', 'occurrence': 1, 'kind': 'concept', 'content_role': 'instruction',
        'chunk_ids': [c.id for c in group], 'body_signature': signature(''.join(c.text for c in group)),
        'identity_signature': signature(''.join(c.text for c in group)), 'review_required': False}
        for path, group in groups.items()] + extra_units
    chunks = filtered + extras
    for page in manifest['pages']:
        page['unit_keys'] = [u['key'] for u in manifest['units'] if any(c.page == page['page'] and c.id in u['chunk_ids'] for c in chunks)]
    return ParsedDocument(chunks, manifest)
