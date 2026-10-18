"""Project only explicit new parser manifests into grounded learning units."""
from hashlib import sha256
import re


SEMANTIC_FIELDS = ('kind', 'parent_id', 'is_learnable', 'content_role', 'learning_objectives',
                   'scope_summary', 'is_assessable', 'structure_key', 'identity_signature', 'extraction')


def unique_units(topics):
    """New units have one automation owner even when both API aliases are selected."""
    result, seen = [], set()
    for topic in topics:
        key = topic.get('canonical_topic_id', topic['id'])
        if 'structure_key' in topic:
            if key in seen:
                continue
            seen.add(key)
        result.append(topic)
    return result


def frozen_unit(topic):
    from copy import deepcopy
    fields = ('id', 'name', 'source_refs', 'automatic_questions') + SEMANTIC_FIELDS
    return {key: deepcopy(topic[key]) for key in fields if key in topic}


def has_learning_units(version):
    manifest = getattr(version, 'parse_manifest', {}) or {}
    return (version.parser_version == manifest.get('parser_version') and manifest.get('schema_version') == 1
            and manifest.get('structure') == 'teaching_units_v1')


def source_statements(source, title):
    from knowpath_backend.learning.materials.document_structure import heading, reference_link
    paragraphs, current = [], ''
    for chunk in source:
        for line in chunk.text.splitlines():
            text = line.strip(' •*-\t')
            if not text or text == title or heading(line) or reference_link(text):
                continue
            # Lists and code remain separate evidence. Ordinary extracted PDF
            # lines join softly, including a sentence continued on another page.
            code = bool(re.search(r'[{};]\s*$', text) or text.startswith('```'))
            boundary = bool(re.match(r'^\s*[•*]\s+', line) or code)
            if boundary:
                if current:
                    paragraphs.append(current)
                    current = ''
                paragraphs.append(text)
            else:
                separator = ' ' if current and (current[-1:].isascii() or text[:1].isascii()) else ''
                current += separator + text
                if re.search(r'[。！？.]$', text):
                    paragraphs.append(current)
                    current = ''
    if current:
        paragraphs.append(current)
    statements = [part.strip() for paragraph in paragraphs for part in re.split(r'(?<=[。！？])', paragraph) if part.strip()]
    return statements


def learning_units(material_id, version):
    chunks = {c.id: c for c in version.chunks}
    units = version.parse_manifest['units']
    identities = {u['key']: 'topic_' + sha256(f"{material_id}:{u['key']}:{u['identity_signature']}:{version.id if u.get('identity_ambiguous') else ''}".encode()).hexdigest()[:24]
                  for u in units}
    result = []
    for unit in units:
        source = [chunks[cid] for cid in unit['chunk_ids'] if cid in chunks]
        # Heading-only Markdown containers can use descendant refs for browsing,
        # while only their direct introductions/leaf units enter automation.
        if not source and unit['kind'] == 'chapter':
            descendants = [u for u in units if u['key'].startswith(unit['key'] + '/')]
            source = [chunks[cid] for u in descendants for cid in u['chunk_ids'] if cid in chunks]
        refs = [{'material_id': material_id, 'material_version_id': version.id, 'chunk_id': c.id,
                 'page': c.page, 'line_start': c.line_start, 'line_end': c.line_end} for c in source]
        from knowpath_backend.learning.materials.document_structure import signature
        statements = source_statements(source, unit['name'])
        teaching_role = unit['content_role'] in {'instruction', 'introduction', 'review'}
        has_body = unit['body_signature'] != signature('') and bool(statements)
        learnable = teaching_role and has_body and bool(refs)
        review = unit.get('review_required', False) or any(
            p.get('review_required') for p in version.parse_manifest['pages'] if p['page'] in {c.page for c in source})
        review = review or (teaching_role and not has_body)
        # Scope/objectives quote bounded source assertions; no invented claim or
        # generic per-unit "understand this section" filler is generated.
        bounded = [s for s in statements if len(s) <= 180 and len(s) >= 8 and re.search(r'[。！？.!?]$', s)]
        scope = ''.join((bounded or statements)[:2])
        if len(scope) > 360:
            scope = f"本节列举「{unit['name']}」的项目和示例，详细范围见下列原文来源。"
        objectives = [('依据原文解释：' + s) for s in bounded[:2]] if learnable else []
        if learnable and not objectives:
            objectives = [f"根据原文，列出「{unit['name']}」中的项目及其给出的特征。"]
        name = ' '.join(unit['name'].split())
        if len(name) > 72:
            name = (unit.get('number') + ' 节内容').strip() if unit.get('number') else '待确认的教学单元'
        result.append({'id': identities[unit['key']], 'name': name,
                       'kind': unit['kind'], 'parent_id': identities.get(unit.get('parent_key')),
                       'level': unit['key'].count('/') if unit['key'].startswith('root/') else 1,
                       'confidence': 0.6 if review else 1.0, 'status': 'active', 'graph_version': 1,
                       'is_learnable': learnable, 'content_role': unit['content_role'],
                       'is_assessable': learnable and not review, 'automatic_questions': learnable and not review,
                       'learning_objectives': objectives, 'scope_summary': scope, 'source_refs': refs, 'prerequisites': [],
                       'structure_key': unit['key'], 'identity_signature': unit['identity_signature'],
                       'extraction': {'method': 'document_structure', 'parser_version': version.parser_version,
                                      'review_required': review,
                                      'identity_mapping': 'unresolved_duplicate' if unit.get('identity_ambiguous') else 'source_signature'}})
    return result
