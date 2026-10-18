"""Teaching units keep semantic boundaries and immutable source provenance."""
from dataclasses import replace

import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning.materials.service import MaterialParser, MaterialService, InMemoryMaterialRepository
from knowpath_backend.learning.persistence.db import Base
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.knowledge.reconciliation import extract_snapshot, compare_snapshots
from knowpath_backend.learning.state import LearningState


TEXT = """# 概率空间

实验结果构成样本空间，事件是样本空间的子集。

## 概述

不可能事件是空集，必然事件是整个样本空间。

### 条件概率

给定事件发生后，检查分母非零。

# 估计方法

估计需要说明样本来源与统计量。

## 概述

样本均值是一次观测得到的统计量。
"""


@pytest.fixture(params=['memory', 'sqlite'])
def unit_repository(request, tmp_path):
    if request.param == 'memory':
        yield InMemoryMaterialRepository()
    else:
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'units.db'}")
        Base.metadata.create_all(engine)
        yield SqlAlchemyMaterialRepository(engine)
        engine.dispose()


def upload(repository, text=TEXT, key='units'):
    return MaterialService(repository).create(filename='probability.md', content=text.encode(), idempotency_key=key)


def test_hierarchy_same_names_introductions_and_persistent_manifest(unit_repository):
    result = upload(unit_repository)
    version = unit_repository.get_version(result.version.id)
    assert version.parse_manifest['schema_version'] == 1
    nodes = extract_snapshot(result.material.id, version)['nodes']
    overviews = [n for n in nodes if n['name'] == '概述']
    assert len(overviews) == 2
    assert len({n['parent_id'] for n in overviews}) == 2
    containers = [n for n in nodes if n['kind'] == 'chapter']
    assert containers and all(not n['is_learnable'] and not n['automatic_questions'] for n in containers)
    learnable = [n for n in nodes if n['is_learnable']]
    assert len([n for n in learnable if n['content_role'] == 'introduction']) == 3
    for node in learnable:
        assert node['source_refs'] and node['learning_objectives'] and node['scope_summary']
        assert node['extraction']['parser_version'] == MaterialParser.VERSION
        assert '\n' not in node['name'] and not node['name'].isdigit()
    assert len({tuple(n['learning_objectives']) for n in learnable}) > 1
    state = LearningState(unit_repository)
    space = state.create_space({'material_ids': [result.material.id]})
    planned = state.plan_sessions._ordered_topics(space, {})
    assessed = state.assessment_service._topics(space)
    assert all(n.get('is_learnable', True) for n in planned)
    assert all(n.get('is_learnable', True) and n['automatic_questions'] for n in assessed)
    assert len({n.get('canonical_topic_id', n['id']) for n in planned}) == len(planned)
    assert len({n.get('canonical_topic_id', n['id']) for n in assessed}) == len(assessed)


def test_display_rename_and_insert_do_not_reassign_identity(unit_repository):
    first = upload(unit_repository)
    service = MaterialService(unit_repository)
    base = extract_snapshot(first.material.id, first.version)
    renamed = service.create_version(material_id=first.material.id, filename='probability.md',
        content=TEXT.replace('# 概率空间', '# 概率基础').replace('### 条件概率', '### 条件事件的概率').encode(), idempotency_key='rename')
    new = extract_snapshot(first.material.id, renamed.version)
    assert {n['id'] for n in base['nodes']} == {n['id'] for n in new['nodes']}
    inserted = service.create_version(material_id=first.material.id, filename='probability.md',
        content=TEXT.replace('## 概述', '## 新问题\n\n抽样单位需要明确定义。\n\n## 概述', 1).encode(), idempotency_key='insert')
    after = extract_snapshot(first.material.id, inserted.version)
    old_by_scope = {n['scope_summary']: n['id'] for n in base['nodes'] if n['is_learnable']}
    for node in after['nodes']:
        if node.get('scope_summary') in old_by_scope:
            assert node['id'] == old_by_scope[node['scope_summary']]
    changed = service.create_version(material_id=first.material.id, filename='probability.md',
        content=TEXT.replace('样本均值是一次观测得到的统计量。', '样本方差估计观测值的离散程度。').encode(), idempotency_key='changed')
    candidate = extract_snapshot(first.material.id, changed.version)
    old = next(n for n in base['nodes'] if n.get('scope_summary') == '样本均值是一次观测得到的统计量。')
    assert old['id'] not in {n['id'] for n in candidate['nodes']}
    assert any(row['id'] == old['id'] for row in compare_snapshots(base, candidate)['removed'])
    assert unit_repository.get_version(first.version.id).chunks == first.version.chunks


def test_pdf_cross_page_units_exact_fragments_and_roles():
    parser = MaterialParser()
    pages = ['学习讲义\n目录\n1\n课程讲义\n1 / 4',
             '一、概率空间\n实验结果构成样本空间。\n1.1 概述\n不可能事件是空集，\n课程讲义\n2 / 4',
             '必然事件是整个样本空间。\n1.2 本节复习\n比较空集与全集。\n二、估计方法\n2.1 概述\n样本均值是统计量。\n课程讲义\n3 / 4',
             '学习总结与综合练习\n解释抽样边界。\n资料来源\nhttps://example.org\n课程讲义\n4 / 4']
    parsed = parser.parse_pages(pages)
    assert all(p['status'] == 'text_extracted' for p in parsed.manifest['pages'])
    from datetime import datetime, timezone
    from knowpath_backend.learning.materials.service import MaterialVersion
    version = MaterialVersion('v', 'm', 'course.pdf', 'application/pdf', 'a'*64, 1, 'ready', datetime.now(timezone.utc),
        parsed.chunks, MaterialParser.VERSION, parsed.manifest)
    snapshot = extract_snapshot('m', version)
    overview = next(n for n in snapshot['nodes'] if n['name'] == '概述' and {r['page'] for r in n['source_refs']} == {2, 3})
    chunks = {c.id: c for c in parsed.chunks}
    assert '必然事件' in ''.join(chunks[r['chunk_id']].text for r in overview['source_refs'])
    assert all('课程讲义' not in chunks[r['chunk_id']].text for n in snapshot['nodes'] if n['is_learnable'] for r in n['source_refs'])
    assert not any(n['is_learnable'] for n in snapshot['nodes'] if any(r['page'] == 1 for r in n['source_refs']))
    assert any(n['content_role'] == 'review' and n['is_learnable'] for n in snapshot['nodes'])
    assert any(n['content_role'] == 'optional_summary' and not n['is_learnable'] for n in snapshot['nodes'])
    assert any('https://example.org' in c.text for c in parsed.chunks)
    for chunk in parsed.chunks:
        assert chunk.text == '\n'.join(pages[chunk.page-1].splitlines()[chunk.line_start-1:chunk.line_end])


def test_ambiguous_explicit_prerequisite_never_chooses_last_overview(unit_repository):
    result = upload(unit_repository, TEXT + '\n## 检验\n\n前置知识：概述\n')
    snapshot = extract_snapshot(result.material.id, result.version)
    assert not [e for e in snapshot['relations'] if e['type'] == 'prerequisite_of']
    assert all(e['to_id'] != e['from_id'] for e in snapshot['relations'])


def test_legacy_without_manifest_keeps_old_names_and_boundaries(unit_repository):
    from knowpath_backend.learning.knowledge.relations import topic_identifier
    result = upload(unit_repository)
    legacy = replace(result.version, parser_version='knowpath_parser_v1', parse_manifest={})
    nodes = extract_snapshot(result.material.id, legacy)['nodes']
    assert len([n for n in nodes if n['name'] == '概述']) == 1
    assert all(n['id'] == topic_identifier(result.material.id, n['name']) for n in nodes)


def test_page_coverage_distinguishes_image_blank_failure_and_mixed_text():
    parsed = MaterialParser().parse_pages(['1.1 样本\n样本来自明确的总体。', '', '', '', '图示说明文字'],
        page_states=[{'status': 'text_extracted'}, {'status': 'ocr_required'}, {'status': 'known_blank'},
                     {'status': 'extraction_failed'}, {'status': 'text_with_images', 'review_required': True}])
    assert [p['status'] for p in parsed.manifest['pages']] == ['text_extracted', 'ocr_required', 'known_blank', 'extraction_failed', 'text_with_images']
    assert parsed.manifest['uncovered_pages'] == [2, 4, 5]
    assert parsed.manifest['complete'] is False


def test_pdf_objectives_exclude_heading_and_join_source_soft_wraps():
    from datetime import datetime, timezone
    from knowpath_backend.learning.materials.service import MaterialVersion
    parsed = MaterialParser().parse_pages(['一、概率\n1.1 条件概率\n给定事件发生后，重新限制样本\n空间并检查分母非零。'])
    version = MaterialVersion('v', 'm', 'p.pdf', 'application/pdf', 'a'*64, 1, 'ready', datetime.now(timezone.utc), parsed.chunks, MaterialParser.VERSION, parsed.manifest)
    node = next(n for n in extract_snapshot('m', version)['nodes'] if n['name'] == '条件概率')
    assert node['scope_summary'] == '给定事件发生后，重新限制样本空间并检查分母非零。'
    assert all('1.1' not in objective and '样本空间' in objective for objective in node['learning_objectives'])


def pdf_with_page_kinds():
    import io
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject, NumberObject
    writer = PdfWriter()
    for width, kind in [(100, 'text'), (200, 'image'), (300, 'blank'), (222, 'failure'), (400, 'mixed'), (500, 'drawing')]:
        page = writer.add_blank_page(width=width, height=100)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        resources = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        operations = b''
        if kind in {'text', 'mixed', 'failure'}:
            operations += b'BT /F1 10 Tf 10 40 Td (1.1 Events) Tj 0 -15 Td (An event is a subset of the sample space.) Tj ET\n'
        if kind in {'image', 'mixed'}:
            image = DecodedStreamObject()
            image.set_data(b'\x00\x00\x00')
            image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'), NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1), NameObject('/ColorSpace'): NameObject('/DeviceRGB'), NameObject('/BitsPerComponent'): NumberObject(8)})
            resources[NameObject('/XObject')] = DictionaryObject({NameObject('/Im0'): writer._add_object(image)})
            operations += b'q 10 0 0 10 0 0 cm /Im0 Do Q'
        if kind == 'drawing':
            operations += b'0 0 20 20 re f'
        page[NameObject('/Resources')] = resources
        if operations:
            stream = DecodedStreamObject()
            stream.set_data(operations)
            page[NameObject('/Contents')] = writer._add_object(stream)
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


def test_real_pdf_partial_page_failure_manifest_persists_and_is_visible(unit_repository, monkeypatch):
    from pypdf._page import PageObject
    from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
    real = PageObject.extract_text
    def extract(page, *args, **kwargs):
        if page.mediabox.width == 222:
            raise ValueError('fixture extraction failure')
        return real(page, *args, **kwargs)
    monkeypatch.setattr(PageObject, 'extract_text', extract)
    state = LearningState(unit_repository)
    original = state.material_service.create(filename='coverage.pdf', content=pdf_with_page_kinds(), idempotency_key='partial', defer_parse=True)
    state.graph_service.enqueue_ingest(original.material.id, original.version.id)
    assert MaterialParseWorker(state.graph_service).run_once()
    version = unit_repository.get_version(original.version.id)
    assert version.chunks and version.status == 'ready'
    assert [p['status'] for p in version.parse_manifest['pages']] == ['text_extracted', 'ocr_required', 'known_blank', 'extraction_failed', 'text_with_images', 'manual_review_required']
    facts = state.graph_service.lifecycle(original.material.id)
    assert facts['parse_manifest']['complete'] is False
    assert facts['parse_manifest']['uncovered_pages'] == [2, 4, 5, 6]
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    with TestClient(create_app(state.material_service)) as client:
        data = client.get('/api/v1/materials/' + original.material.id).json()
        assert data['version']['parse_manifest'] == version.parse_manifest


def test_failed_document_manifest_is_persisted(unit_repository):
    from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
    state = LearningState(unit_repository)
    original = state.material_service.create(filename='broken.pdf', content=b'%PDF-unreadable', idempotency_key='broken', defer_parse=True)
    state.graph_service.enqueue_ingest(original.material.id, original.version.id)
    MaterialParseWorker(state.graph_service).run_once()
    version = unit_repository.get_version(original.version.id)
    assert version.status == 'failed' and version.parse_manifest['document_status'] == 'extraction_failed'
    assert version.parse_manifest['complete'] is False


def publish(state, result, expected, key, action=None):
    from knowpath_backend.test.test_learning_graph_worker import worker, event_id
    staged = state.graph_service.reconcile(result.material.id, {'version_id': result.version.id, 'expected_graph_version': expected}, key+'-stage')
    worker(state).run_once(event_id(state, staged))
    revision = state.graph_service.repository.get_record('graph_revisions', staged['candidate_revision_id'])
    resolutions = [{'conflict_id': row['conflict_id'], 'action': action, 'reason': 'checked immutable sources'} for row in revision['diff']['conflicts']]
    state.graph_service.publish(result.material.id, revision['id'], {'expected_graph_version': expected, 'resolutions': resolutions}, key+'-publish')
    return state.graph_service.repository.get_record('graph_revisions', revision['id'])


@pytest.mark.parametrize('action', ['keep_old', 'keep_both'])
def test_new_units_rename_review_keeps_old_sources_and_old_space(unit_repository, action):
    state = LearningState(unit_repository)
    first = upload(unit_repository)
    old = publish(state, first, 0, 'old')
    space = state.create_space({'material_ids': [first.material.id]})
    before = state.space_service.bound_topics(space)
    changed = state.material_service.create_version(material_id=first.material.id, filename='probability.md',
        content=TEXT.replace('### 条件概率', '### 条件事件概率').encode(), idempotency_key='rename-review')
    new = publish(state, changed, 1, 'renamed', action)
    assert new['diff']['conflicts']
    old_ids = {c.id for c in first.version.chunks}
    snapshot = new['publication']['snapshot']
    assert old_ids & {s['id'] for s in snapshot['sources']}
    assert all(ref['chunk_id'] in {s['id'] for s in snapshot['sources']} for n in snapshot['nodes'] for ref in n['source_refs'])
    assert state.space_service.bound_topics(state.get_space(space['id'])) == before
    assert old['publication']['snapshot'] == state.graph_service.repository.get_record('graph_revisions', old['id'])['publication']['snapshot']
    if action == 'keep_both':
        assert any(n.get('alternatives') and n['automatic_questions'] is False for n in snapshot['nodes'])


def test_legacy_repeat_upload_reports_upgrade_and_explicit_reparse_keeps_space(unit_repository):
    from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
    from knowpath_backend.learning.materials.ingestion import MaterialIngestionService
    class Legacy:
        VERSION = 'knowpath_parser_v1'
        def parse(self, content, *, filename):
            return MaterialParser()._parse_text(content.decode())
    service = MaterialService(unit_repository, Legacy())
    old = service.create(filename='old.md', content=TEXT.encode(), idempotency_key='legacy')
    state = LearningState(unit_repository)
    space = state.create_space({'material_ids': [old.material.id]})
    before = state.space_service.bound_topics(space)
    ingestion = MaterialIngestionService(state.material_service, state.run_service, state.graph_service)
    repeated = ingestion.create(filename='old.md', content=TEXT.encode(), idempotency_key='repeat')
    assert repeated.resource.version.id == old.version.id
    assert unit_repository.get_version(old.version.id).chunks == old.version.chunks
    assert state.graph_service.lifecycle(old.material.id)['parser_upgrade_available'] is True
    new = state.graph_service.reparse(old.material.id, {'version_id': old.version.id}, 'explicit')
    assert new['material_version_id'] != old.version.id
    MaterialParseWorker(state.graph_service).run_once()
    assert unit_repository.get_version(new['material_version_id']).parse_manifest['units']
    assert state.space_service.bound_topics(state.get_space(space['id'])) == before


def test_legal_new_parser_keeps_article_boundaries_but_excludes_cover_footer():
    from datetime import datetime, timezone
    from knowpath_backend.learning.materials.service import MaterialVersion
    pages = ['示例法\n公布说明\n学习法条\n1 / 3',
             '第一章 总则\n第一条 第一项规定。\n第二条 跨页前半\n学习法条\n2 / 3',
             '后半完整规定。\n第三条 下一项。\n学习法条\n3 / 3']
    parsed = MaterialParser().parse_pages(pages)
    version = MaterialVersion('v', 'm', 'law.pdf', 'application/pdf', 'a'*64, 1, 'ready', datetime.now(timezone.utc), parsed.chunks, MaterialParser.VERSION, parsed.manifest)
    nodes = extract_snapshot('m', version)['nodes']
    chunks = {c.id: c for c in parsed.chunks}
    learning = [n for n in nodes if n['is_learnable']]
    assert learning and not any(ref['page'] == 1 for n in learning for ref in n['source_refs'])
    assert all('学习法条' not in chunks[ref['chunk_id']].text for n in learning for ref in n['source_refs'])
    assert any({ref['page'] for ref in n['source_refs']} == {2, 3} for n in learning)


def test_reference_block_ends_at_next_teaching_heading(unit_repository):
    result = upload(unit_repository, '来源说明\nhttps://example.org\n一、正式章节\n实验证据需要记录来源。\n1.1 观测\n观测值应当同时记录时间和单位。')
    nodes = extract_snapshot(result.material.id, result.version)['nodes']
    assert any(n['name'] == '观测' and n['is_learnable'] for n in nodes)
    source = {c.id: c.text for c in result.version.chunks}
    assert not any('https://' in source[r['chunk_id']] for n in nodes if n['is_learnable'] for r in n['source_refs'])


def test_duplicate_numbering_cannot_inherit_identity_across_versions(unit_repository):
    content = '一、概率\n1.1 概述\n同样的实验边界说明。\n1.1 概述\n同样的实验边界说明。'
    first = upload(unit_repository, content)
    second = MaterialService(unit_repository).create_version(material_id=first.material.id, filename='probability.md',
        content=(content + '\n1.2 检查\n检查所有观测是否完整。').encode(), idempotency_key='ambiguous')
    old = [n for n in extract_snapshot(first.material.id, first.version)['nodes'] if n['name'] == '概述']
    new = [n for n in extract_snapshot(first.material.id, second.version)['nodes'] if n['name'] == '概述']
    assert len(old) == len(new) == 2
    assert not {n['id'] for n in old} & {n['id'] for n in new}
    assert all(n['extraction']['review_required'] and not n['automatic_questions'] for n in new)


def test_inline_comparison_is_prose_and_does_not_cut_objective():
    from knowpath_backend.learning.knowledge.learning_units import source_statements
    source = MaterialParser()._parse_text('自动拆箱遇到 null\n会产生异常，比较也不应简单依赖 ==。\n边界应当依据类型契约检查。')
    assert source_statements(source, '类型边界')[0] == '自动拆箱遇到 null 会产生异常，比较也不应简单依赖 ==。'


def test_plan_freezes_unit_boundary_metadata_and_explicit_alias_is_not_duplicated(unit_repository):
    first = upload(unit_repository)
    state = LearningState(unit_repository)
    space = state.create_space({'material_ids': [first.material.id]})
    topic = next(t for t in state.space_service.bound_topics(space) if t['is_learnable'] and t['id'] == t['canonical_topic_id'])
    alias = next(t for t in state.space_service.bound_topics(space) if t['canonical_topic_id'] == topic['id'] and t['id'] != topic['id'])
    state.set_scope(space['id'], {'topic_ids': [topic['id'], alias['id']], 'expected_version': 1})
    space = state.get_space(space['id'])
    assert len(state.plan_sessions._ordered_topics(space, {})) == 1
    assert len(state.assessment_service._topics(space)) == 1
    assert {source['topic_id'] for source in state.message_service._sources(space)} == {topic['id']}
    plan = state.create_plan(space['id'], {})
    frozen = plan['tasks'][0]['context']['topics'][0]
    assert frozen['scope_summary'] == topic['scope_summary']
    assert frozen['learning_objectives'] == topic['learning_objectives']
    assert frozen['content_role'] == topic['content_role']
    record = state.plan_sessions.get_learning_record(plan['id'], plan['tasks'][0]['id'])
    assert record['topics'][0]['learning_objectives'] == topic['learning_objectives']


def test_markdown_fenced_code_is_body_and_not_a_teaching_heading(unit_repository):
    text = '# 脚本\n\n脚本按顺序执行。\n\n```python\n# 注释\n1. 这不是章节\nprint("https://example.test")\n```\n\n~~~sh\n# Shell 注释\necho done\n~~~\n\n## 检查\n\n检查程序的返回状态。'
    result = upload(unit_repository, text)
    nodes = extract_snapshot(result.material.id, result.version)['nodes']
    assert {n['name'] for n in nodes} == {'脚本', '脚本 · 导言', '检查'}
    intro = next(n for n in nodes if n['content_role'] == 'introduction')
    fragments = {c.id: c.text for c in result.version.chunks}
    body = '\n'.join(fragments[r['chunk_id']] for r in intro['source_refs'])
    assert '# 注释' in body and '1. 这不是章节' in body and 'https://example.test' in body


def test_changed_body_adoption_invalidates_old_mastery_without_rewriting_history(unit_repository):
    from copy import deepcopy
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions, create, answer
    state = LearningState(unit_repository, question_generator=FixedQuestions())
    first = upload(unit_repository, '# 观测\n\n记录观测的时间与单位。')
    old = publish(state, first, 0, 'body-old')
    space = state.create_space({'material_ids': [first.material.id]})
    old_topic = next(t for t in state.space_service.bound_topics(space) if t['id'] == t['canonical_topic_id'])
    assessment = create(state, space['id'], 'body-assessment')
    answer(state, assessment, 'body-answer', finalize=True)
    repository = state.assessment_service.repository
    old_state = repository.records('states', space_id=space['id'])[0]
    old_state['status'] = 'mastered'
    repository.put_record('states', old_state)
    questions = deepcopy(repository.get_record('assessments', assessment['id'])['questions'])
    evidence = deepcopy(repository.records('evidence', space_id=space['id']))
    changed = state.material_service.create_version(material_id=first.material.id, filename='observations.md',
        content='# 观测\n\n记录观测的条件并说明误差范围。'.encode(), idempotency_key='body-new')
    new = publish(state, changed, 1, 'body-new')
    new_topic = new['publication']['snapshot']['nodes'][0]
    assert new_topic['id'] != old_topic['id']
    assert state.get_space(space['id'])['bindings'] == space['bindings']
    assert repository.records('states', space_id=space['id'])[0]['status'] == 'mastered'
    preview = state.knowledge_updates(space['id'])
    assert {old_topic['id'], new_topic['id']} <= set(preview['affected_topic_ids'])
    result = state.apply_knowledge_updates(space['id'], {'bindings': [{'material_id': first.material.id,
        'material_version_id': changed.version.id, 'graph_version': 2}], 'expected_space_version': 1}, idempotency_key='body-adopt')
    assert result['stale_state_count'] == 1
    current = repository.records('states', space_id=space['id'])
    assert current[0]['score_validity'] == 'stale' and current[0]['status'] != 'mastered'
    assert not any(s['topic_id'] == new_topic['id'] and s['status'] == 'mastered' for s in current)
    assert current[0]['mastery_score'] == old_state['mastery_score']
    assert repository.get_record('assessments', assessment['id'])['questions'] == questions
    assert repository.records('evidence', space_id=space['id']) == evidence
    assert old['publication']['snapshot'] == state.graph_service.repository.get_record('graph_revisions', old['id'])['publication']['snapshot']


def test_new_units_custom_order_prerequisite_fixed_node_and_replan(unit_repository, monkeypatch):
    from copy import deepcopy
    result = upload(unit_repository, '# 采样\n\n采样应当说明边界。\n\n# 记录\n\n记录应当保留单位。\n\n# 复核\n\n复核应当比较原始记录。\n\n# 汇总\n\n汇总应当标明缺失值。')
    state = LearningState(unit_repository)
    space = state.create_space({'material_ids': [result.material.id]})
    topics = [t for t in state.space_service.bound_topics(space) if t['id'] == t['canonical_topic_id']]
    state.set_scope(space['id'], {'topic_ids': [t['id'] for t in topics], 'expected_version': 1})
    original = state.space_service.bound_topics
    def dependency(current):
        rows = original(current)
        next(t for t in rows if t['id'] == topics[1]['id'])['prerequisites'] = [topics[0]['id']]
        return rows
    monkeypatch.setattr(state.space_service, 'bound_topics', dependency)
    plan = state.create_plan(space['id'], {})
    assert len(plan['tasks']) == 4
    before = deepcopy(state.get_plan(plan['id']))
    conflict = state.preview_plan_order(plan['id'], {'expected_plan_version': 1, 'order_mode': 'custom',
        'ordered_node_ids': [t['node_id'] for t in reversed(plan['tasks'])]})
    assert not conflict['can_apply'] and state.get_plan(plan['id']) == before
    chosen = [plan['tasks'][i]['node_id'] for i in [0, 2, 1, 3]]
    ordered = state.update_plan_order(plan['id'], {'expected_plan_version': 1, 'order_mode': 'custom', 'ordered_node_ids': chosen})
    assert [t['node_id'] for t in ordered['tasks']] == chosen
    rebuilt = state.create_plan(space['id'], {'rebuild_mode': 'local_replan', 'base_plan_id': plan['id'], 'expected_plan_version': ordered['version']})
    assert [t['node_id'] for t in rebuilt['tasks']] == chosen
    state.start_session(rebuilt['id'], rebuilt['tasks'][0]['id'])
    current = state.get_plan(rebuilt['id'])
    fixed = state.preview_plan_order(current['id'], {'expected_plan_version': current['version'], 'order_mode': 'custom',
        'ordered_node_ids': [t['node_id'] for t in reversed(current['tasks'][1:])]})
    assert fixed['fixed_nodes'][0]['reason'] == 'active_session'
    assert fixed['candidate_plan']['tasks'][0]['node_id'] == chosen[0]


@pytest.mark.parametrize('drawing,review', [
    (b'0.5 w 0 5 m 100 5 l S', False),
    (b'0 0 100 1 re f', False),
    (b'40 w 0 0 100 1 re B', True),
    (b'0 0 40 40 re f', True),
    (b'0 0 m 20 40 40 0 60 40 c S', True),
    (b'0 1 -1 0 0 0 cm 0 5 m 100 5 l S', True),
])
def test_real_pdf_mixed_vectors_require_review_except_simple_rules(drawing, review):
    import io
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=150)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 10 Tf 10 100 Td (1.1 Shape) Tj 0 -15 Td (This chart defines the sample space.) Tj ET\n' + drawing)
    page[NameObject('/Contents')] = writer._add_object(stream)
    target = io.BytesIO()
    writer.write(target)
    repository = InMemoryMaterialRepository()
    result = MaterialService(repository).create(filename='diagram.pdf', content=target.getvalue(), idempotency_key='vectors')
    manifest = result.version.parse_manifest
    assert manifest['pages'][0]['graphics_present'] is True
    assert manifest['pages'][0]['review_required'] is review
    assert manifest['pages'][0]['status'] == ('text_with_graphics' if review else 'text_extracted')
    assert manifest['complete'] is not review
    node = extract_snapshot(result.material.id, result.version)['nodes'][0]
    assert node['is_learnable'] and node['is_assessable'] is not review and node['automatic_questions'] is not review
    state = LearningState(repository)
    space = state.create_space({'material_ids': [result.material.id]})
    plan = state.create_plan(space['id'], {})
    assert state.start_session(plan['id'], plan['tasks'][0]['id'])['status'] == 'active'


def pdf_page_snapshot(pages):
    from datetime import datetime, timezone
    from knowpath_backend.learning.materials.service import MaterialVersion
    parsed = MaterialParser().parse_pages(pages)
    version = MaterialVersion('version', 'material', 'guide.pdf', 'application/pdf', 'a'*64, 1, 'ready',
        datetime.now(timezone.utc), parsed.chunks, MaterialParser.VERSION, parsed.manifest)
    return version, extract_snapshot('material', version)['nodes']


def test_teaching_prose_with_url_retains_source_and_grounded_objective():
    sentence = '使用 https://example.org 访问服务时，客户端必须验证证书链及域名。'
    version, nodes = pdf_page_snapshot(['1.1 安全通信\n' + sentence])
    node = next(n for n in nodes if n['name'] == '安全通信')
    chunks = {c.id: c for c in version.chunks}
    assert sentence in '\n'.join(chunks[r['chunk_id']].text for r in node['source_refs'])
    assert sentence in node['scope_summary']
    assert any(sentence in goal for goal in node['learning_objectives'])
    assert node['is_learnable'] and node['is_assessable']


@pytest.mark.parametrize('markdown', [False, True])
def test_explicit_table_of_contents_preserves_sources_without_polluting_body_identity(markdown):
    cover = '目录\n一、概率基础\n1.1 样本空间 ........ 2\n1.2 条件概率 ........ 3'
    body = '一、概率基础\n概率描述观测的不确定性。\n1.1 样本空间\n样本空间包含全部可能的实验结果。\n1.2 条件概率\n条件概率重新限制事件发生的样本空间。'
    if markdown:
        parsed = MaterialParser().parse_document((cover+'\n\n'+body).encode(), filename='guide.md')
        from datetime import datetime, timezone
        from knowpath_backend.learning.materials.service import MaterialVersion
        version = MaterialVersion('version','material','guide.md','text/markdown','b'*64,1,'ready',datetime.now(timezone.utc),parsed.chunks,MaterialParser.VERSION,parsed.manifest)
        nodes = extract_snapshot('material', version)['nodes']
    else:
        version, nodes = pdf_page_snapshot([cover, body])
    source = [n for n in nodes if n['content_role'] == 'front_matter']
    assert source and not any(n['is_learnable'] or n['automatic_questions'] for n in source)
    teaching = [n for n in nodes if n['is_learnable']]
    assert {n['name'] for n in teaching} == {'概率基础 · 导言','样本空间','条件概率'}
    assert all(n['is_assessable'] and not n['extraction']['review_required'] for n in teaching)
    assert not any('........' in n['name'] for n in nodes)
    text = '\n'.join(c.text for c in version.chunks)
    assert '1.1 样本空间 ........ 2' in text and '1.2 条件概率 ........ 3' in text


def test_heading_only_leaf_has_sources_but_no_learning_or_assessment():
    version, nodes = pdf_page_snapshot(['1.1 待补充\n1.2 有内容\n这节说明如何核对观测结果。'])
    empty = next(n for n in nodes if n['name'] == '待补充')
    assert empty['source_refs'] and not empty['is_learnable'] and not empty['is_assessable']
    assert not empty['automatic_questions'] and empty['learning_objectives'] == []
    assert empty['extraction']['review_required']


def test_contents_ends_before_body_chapter_even_without_direct_introduction():
    _, nodes = pdf_page_snapshot(['目录\n一、概率基础\n1.1 样本空间 ........ 2\n1.2 条件概率 ........ 3',
        '一、概率基础\n1.1 样本空间\n样本空间包含全部可能的实验结果。\n1.2 条件概率\n条件概率重新限制事件发生的样本空间。'])
    parent = next(n for n in nodes if n['name'] == '概率基础' and n['kind'] == 'chapter')
    children = [n for n in nodes if n['is_learnable']]
    assert len(children) == 2 and all(n['parent_id'] == parent['id'] and n['is_assessable'] for n in children)


def test_teaching_url_and_standalone_reference_link_have_distinct_roles():
    sentence = '访问 https://example.org 时必须核对证书。'
    version, nodes = pdf_page_snapshot(['1.1 安全通信\n' + sentence + '\nhttps://reference.test\n1.2 检查\n检查证书链和服务域名。'])
    chunks = {c.id:c.text for c in version.chunks}
    reference = next(n for n in nodes if n['content_role'] == 'reference')
    assert 'https://reference.test' in '\n'.join(chunks[r['chunk_id']] for r in reference['source_refs'])
    assert not reference['is_learnable']
    assert any(sentence in n['scope_summary'] for n in nodes if n['is_learnable'])
