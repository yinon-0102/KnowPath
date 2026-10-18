"""Deterministic teaching-document structure, independent of subject vocabulary.

Pages and lines remain source coordinates. A section can span pages; its direct
body becomes an introduction when it also has children. No document-order
prerequisites or generated factual claims are introduced here.
"""
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import math
import re
from uuid import uuid4


@dataclass(frozen=True)
class ParsedDocument:
    chunks: list
    manifest: dict


class ParsedChunks(list):
    """The longstanding parse() list port, with per-call immutable provenance.

    Keeping metadata on the returned value avoids shared parser-instance state
    and preserves subclasses that instrument/override parse().
    """
    def __init__(self, document):
        super().__init__(document.chunks)
        self.manifest = document.manifest


def signature(text):
    return sha256(re.sub(r'\s+', '', text).encode()).hexdigest()[:24]


def heading(line):
    text = line.strip()
    match = re.fullmatch(r'(#{1,6})\s+(.+?)\s*#*', text)
    if match:
        return len(match[1]), '', match[2].strip()
    match = re.fullmatch(r'([〇零一二三四五六七八九十百千万两]+)[、．]\s*(\S.{0,70})', text)
    if match:
        return 1, match[1], match[2].strip()
    match = re.fullmatch(r'第\s*([〇零一二三四五六七八九十百千万两0-9]+)\s*([章节篇部])\s*(.{0,70})', text)
    if match:
        return (2 if match[2] == '节' else 1), match[1], match[3].strip() or text
    match = re.fullmatch(r'(\d+(?:\.\d+)+)\s+([^\d\s].{0,70})', text)
    if match:
        return len(match[1].split('.')), match[1], match[2].strip()
    match = re.fullmatch(r'(\d+)[.)、]\s+([^\d\s].{0,70})', text)
    if match and not re.search(r'https?://', text):
        return 1, match[1], match[2].strip()
    if len(text) <= 32 and re.fullmatch(r'(?:学习)?总结(?:与.*|及.*)?|综合练习(?:与.*|路线)?|复习与练习', text):
        return 1, '', text
    return None


def content_role(title):
    if re.search(r'总结|综合练习', title):
        return 'optional_summary'
    if re.search(r'复习|自测|回顾|Review|Exercises', title, re.I):
        return 'review'
    return 'instruction'


def reference_link(line):
    """Standalone source links are not the same as teaching prose using URLs."""
    return bool(re.fullmatch(r'(?:[-*•]\s*)?(?:(?:来源|参考链接|参考来源|Source|Reference)\s*[:：]\s*)?(?:https?://\S+|\[[^\]]+\]\(https?://[^\s)]+\))[。；;]?', line.strip(), re.I))


def structure_document(pages, *, parser_version, page_states=None, markdown=False, pdf=True):
    from .service import SourceChunk

    all_lines = [text.replace('\r\n', '\n').replace('\r', '\n').splitlines() for text in pages]
    margins = Counter()
    if pdf:
        for lines in all_lines:
            nonempty = [(i, s.strip()) for i, s in enumerate(lines) if s.strip()]
            margins.update({s for _, s in nonempty[-3:] if len(s) <= 100})
    repeated = {s for s, count in margins.items() if count >= max(2, math.ceil(len(pages) / 2))}
    sections, stack, extras = [], [], {}
    seen_heading = False
    reference_section = False
    reference_level = None
    fence = None
    contents = False
    contents_seen = False
    contents_headings = set()
    flattened = [(p, i) for p, lines in enumerate(all_lines) for i in range(len(lines))]
    next_text = {}
    following = ''
    for p, i in reversed(flattened):
        next_text[(p, i)] = following
        candidate = all_lines[p][i].strip()
        if candidate and not re.fullmatch(r'[-—]?\s*\d+\s*[-—]?|\d+\s*[/／]\s*\d+', candidate):
            following = candidate

    def extra(role, row):
        extras.setdefault(role, []).append(row)

    for page, lines in enumerate(all_lines, 1):
        nonempty = [i for i, line in enumerate(lines) if line.strip()]
        bottom = set(nonempty[-3:])
        for index, line in enumerate(lines):
            row = (page if pdf else None, index + 1, line)
            text = line.strip()
            marker = re.match(r'^(`{3,}|~{3,})(.*)$', text) if markdown else None
            if markdown and (fence or marker):
                if reference_section:
                    extra('reference', row)
                elif stack:
                    stack[-1]['body'].append(row)
                else:
                    extra('instruction', row)
                if fence:
                    if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                        fence = None
                elif marker:
                    fence = marker[1]
                continue
            if pdf and index in bottom and (text in repeated or re.fullmatch(r'(?:第\s*)?\d+\s*(?:[/／]|of)\s*\d+\s*(?:页)?', text, re.I) or re.fullmatch(r'[-—]?\s*\d+\s*[-—]?', text)):
                extra('footer', row)
                continue
            parsed = heading(line)
            title = parsed[2] if parsed else text
            if re.fullmatch(r'目\s*录|(?:Table\s+of\s+)?Contents', title, re.I):
                contents, contents_seen = True, True
                stack.clear()
            if contents:
                leader = bool(re.search(r'(?:\.{2,}|…+|·{2,}|\s{2,})\s*\d+\s*$', text))
                following = next_text[(page - 1, index)]
                # A real section followed by prose starts the teaching body.
                # TOC entries/leader rows never become section identity anchors.
                repeated_section = text in contents_headings and not re.search(r'(?:\.{2,}|…+|·{2,})\s*\d+\s*$', following)
                actual_section = parsed and not leader and following and (
                    repeated_section or not heading(following) and not re.search(r'(?:\.{2,}|…+|·{2,})\s*\d+\s*$', following))
                if actual_section or (text and not parsed and not leader and not re.fullmatch(r'目\s*录|(?:Table\s+of\s+)?Contents', title, re.I)):
                    contents = False
                else:
                    if parsed:
                        contents_headings.add(text)
                    extra('front_matter', row)
                    continue
            reference_title = parsed[2] if parsed else text
            is_reference = bool(re.fullmatch(r'(?:资料|参考|内容)?来源(?:说明|网址|链接)?[：:]?|参考(?:资料|文献|链接)[：:]?', reference_title))
            # A numbered bibliography entry is not a teaching heading. Explicit
            # Markdown/Chinese/decimal sections end the source block at its level.
            if reference_section and parsed and not is_reference and (
                    text.startswith('#') or '.' in parsed[1] or (parsed[1] and not parsed[1].isdigit())) and (
                    reference_level is None or parsed[0] <= reference_level):
                reference_section = False
                reference_level = None
            if is_reference:
                reference_section = True
                reference_level = parsed[0] if parsed else None
            if reference_section or reference_link(text):
                extra('reference', row)
                continue
            if parsed:
                level, number, title = parsed
                while stack and stack[-1]['level'] >= level:
                    stack.pop()
                section = {'name': title, 'number': number, 'level': level,
                           'parent': stack[-1] if stack else None, 'children': [],
                           'heading': [] if markdown else [row], 'body': [], 'role': content_role(title)}
                if stack:
                    stack[-1]['children'].append(section)
                sections.append(section)
                stack.append(section)
                seen_heading = True
            elif stack:
                stack[-1]['body'].append(row)
            elif text or not seen_heading:
                extra('front_matter' if pdf else 'instruction', row)

    chunks, units = [], []

    def fragments(rows, path):
        result, batch = [], []
        def flush():
            if batch:
                body = '\n'.join(row[2] for row in batch)
                chunk = SourceChunk(str(uuid4()), body, tuple(path), batch[0][1], batch[-1][1], batch[0][0],
                                    sha256(body.encode()).hexdigest())
                chunks.append(chunk)
                result.append(chunk.id)
                batch.clear()
        for row in rows:
            if not row[2].strip():
                flush()
                continue
            if batch and (row[0] != batch[-1][0] or row[1] != batch[-1][1] + 1):
                flush()
            batch.append(row)
        flush()
        return result

    def body_signature(section):
        return signature('\n'.join(row[2] for row in section['body']))

    # Stable parent anchors prefer explicit numbering, then the parent's own
    # introduction. Child insertion/reordering cannot move these anchors.
    for section in reversed(sections):
        own = body_signature(section)
        section['anchor'] = ('n:' + section['number'] if section['number'] else
                             'b:' + own if any(row[2].strip() for row in section['body']) else
                             'c:' + signature('|'.join(sorted(c['anchor'] for c in section['children']))))
    occurrences = Counter()
    sibling_counts = Counter((id(s['parent']), s['anchor']) for s in sections)
    for section in sections:
        parent = section['parent']
        parent_key = parent['key'] if parent else 'root'
        anchor = section['anchor']
        occurrences[(parent_key, anchor)] += 1
        occurrence = occurrences[(parent_key, anchor)]
        section['key'] = f'{parent_key}/{anchor}:{occurrence}'
        path = [section['name']]
        ancestor = parent
        while ancestor:
            path.insert(0, ancestor['name'])
            ancestor = ancestor['parent']
        ambiguous = sibling_counts[(id(parent), anchor)] > 1
        section['identity_ambiguous'] = ambiguous or bool(parent and parent['identity_ambiguous'])
        container = bool(section['children'])
        rows = section['heading'] if container else section['heading'] + section['body']
        ids = fragments(rows, path)
        units.append({'key': section['key'], 'parent_key': parent_key if parent else None,
                      'name': section['name'], 'number': section['number'], 'occurrence': occurrence,
                      'kind': 'chapter' if container else 'concept',
                      'content_role': 'container' if container else section['role'],
                      'chunk_ids': ids, 'body_signature': body_signature(section),
                      'identity_signature': section['anchor'] if container else body_signature(section),
                      'identity_ambiguous': section['identity_ambiguous'],
                      'review_required': section['identity_ambiguous'] or len(section['name']) > 72})
        if container and any(row[2].strip() for row in section['body']):
            units.append({'key': section['key'] + '/introduction', 'parent_key': section['key'],
                          'name': section['name'] + ' · 导言', 'number': '', 'occurrence': 1,
                          'kind': 'concept', 'content_role': 'introduction',
                          'chunk_ids': fragments(section['body'], path),
                          'body_signature': body_signature(section), 'identity_signature': body_signature(section),
                          'identity_ambiguous': section['identity_ambiguous'],
                          'review_required': section['identity_ambiguous']})

    for role, rows in extras.items():
        # A source without any recognized headings still has an inspectable,
        # bounded body, rather than a title copied from an arbitrary sentence.
        if role == 'front_matter' and not sections and not contents_seen:
            role = 'instruction'
        names = {'footer': '页眉页脚', 'reference': '参考来源', 'front_matter': '封面与目录', 'instruction': '正文'}
        ids = fragments(rows, (names[role],))
        if ids:
            key = 'source:' + role
            units.append({'key': key, 'parent_key': None, 'name': names[role], 'number': '', 'occurrence': 1,
                          'kind': 'concept' if role == 'instruction' else 'source', 'content_role': role,
                          'chunk_ids': ids, 'body_signature': signature('\n'.join(row[2] for row in rows)),
                          'identity_signature': signature('\n'.join(row[2] for row in rows)),
                          'review_required': role == 'instruction'})

    coverage = []
    for page, text in enumerate(pages, 1):
        state = dict(page_states[page - 1]) if page_states else {'status': 'text_extracted' if text.strip() else 'known_blank'}
        state.update(page=page if pdf else None, line_count=len(all_lines[page - 1]),
                     unit_keys=[u['key'] for u in units if any(c.id in u['chunk_ids'] and c.page == (page if pdf else None) for c in chunks)])
        coverage.append(state)
    uncovered = [p['page'] for p in coverage if p['status'] not in {'text_extracted', 'known_blank'} or p.get('review_required')]
    manifest = {'schema_version': 1, 'structure': 'teaching_units_v1', 'parser_version': parser_version,
                'pages': coverage, 'uncovered_pages': uncovered, 'complete': not uncovered, 'units': units}
    return ParsedDocument(chunks, manifest)
