"""Transactional notebooks: facts are deterministic, prose is model generated."""
from __future__ import annotations
import copy
import hashlib
import json
import re
from uuid import uuid4
from datetime import datetime, timezone
from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.workers.model_tasks import enqueue

def uid(): return str(uuid4())
def now(): return datetime.now(timezone.utc).isoformat()
def fingerprint(value): return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

class NotesService:
    def __init__(self, repository, spaces, assessments, plans, runs, generator=None):
        self.repository, self.spaces, self.assessments, self.plans, self.runs = repository, spaces, assessments, plans, runs
        if generator is None:
            from knowpath_backend.learning.notes.generation import ConfiguredNoteGenerator
            generator = ConfiguredNoteGenerator()
        self.generator = generator

    def _put(self, table, row):
        self.repository.put_record(table, row)
        return copy.deepcopy(row)

    def _save(self, chapter, reason):
        prior = self.repository.get_record('note_chapters', chapter['id'])
        self._put('note_revisions', dict(prior, id=uid(), chapter_id=chapter['id'], reason=reason))
        chapter['version'] += 1
        chapter['updated_at'] = now()
        return self._put('note_chapters', chapter)

    def register_plan(self, space_id):
        with self.repository.transaction():
            space = self.spaces.repository.get(space_id)
            books = self.repository.records('notebooks', space_id=space_id)
            book = books[0] if books else {'id': uid(), 'space_id': space_id, 'name': space['name'], 'directory': 'review-notes/' + re.sub(r'[^\w\-]+', '-', space['name']).strip('-') + '-' + space_id[:8] + '/', 'rounds': {}, 'created_at': now()}
            book['name'] = space['name']
            existing = {c['node_id']: c for c in self.repository.records('note_chapters', space_id=space_id)}
            plans = self.plans._space_plans(space_id)
            canonical = self.plans._canonical_nodes(plans, self.plans._space_sessions(space_id))
            # Canonical grouping identifies the originating round even for old
            # copies created in the same timestamp with reversed UUID order.
            by_node_origin = {n['node_id']: pid for pid, nodes in canonical.items() for n in nodes}
            by_id = {p['id']: p for p in plans}
            def depth(plan, seen=frozenset()):
                if plan['id'] in seen: return 0
                parents = {by_node_origin[t['node_id']] for t in plan['tasks'] if t['node_id'] in by_node_origin and by_node_origin[t['node_id']] != plan['id']}
                if plan.get('config', {}).get('base_plan_id') in by_id: parents.add(plan['config']['base_plan_id'])
                return max((1 + depth(by_id[pid], seen | {plan['id']}) for pid in parents), default=0)
            ordered = sorted(plans, key=lambda p: (depth(p), self.plans._plan_key(p)))
            for plan in ordered:
                book['rounds'].setdefault(plan['id'], len(book['rounds']) + 1)
            for plan in ordered:
                pid = plan.get('id', plan.get('plan_id'))
                book['rounds'].setdefault(pid, len(book['rounds']) + 1)
                for index, task in enumerate(plan['tasks'], 1):
                    context = task.get('context', {})
                    node = task.get('node_id', context.get('node_id', task['id']))
                    if node in existing: continue
                    origin_id = by_node_origin.get(node, pid)
                    origin_plan = next(p for p in plans if p['id'] == origin_id)
                    origin_task = next(t for t in origin_plan['tasks'] if t['node_id'] == node)
                    context = origin_task.get('context', {})
                    title = context.get('title') or '、'.join(t['name'] for t in context.get('topics', [])) or '学习小节'
                    title = re.sub(r'[\\/:*?"<>|]', '-', title)
                    number = f"{book['rounds'][origin_id]}.{origin_task.get('sequence', context.get('sequence', index))}"
                    chapter = {'id': uid(), 'space_id': space_id, 'node_id': node, 'plan_id': pid, 'task_id': task['id'], 'number': number, 'title': title, 'filename': number + '-' + title + '.md', 'status': 'IN_PROGRESS', 'generation_status': '未总结', 'version': 1, 'blocks': [], 'corrections': [], 'correction_count': 0, 'topic_ids': copy.deepcopy(task['topic_ids']), 'source_refs': copy.deepcopy(context.get('source_refs', [])), 'created_at': now(), 'updated_at': now()}
                    self._put('note_chapters', chapter)
                    existing[node] = chapter
            self._put('notebooks', book)
            return self._notebook(book)

    def _notebook(self, book):
        chapters = self.repository.records('note_chapters', space_id=book['space_id'])
        chapters.sort(key=lambda c: tuple(int(p) for p in c['number'].split('.')))
        completed = sum(c['status'] == 'COMPLETED' for c in chapters)
        lines = ['# ' + book['name'], '', '| 章节 | 状态 | 纠偏数量 | 更新时间 |', '| --- | --- | --- | --- |']
        for c in chapters:
            lines.append(f"| [{c['number']} {c['title']}]({c['filename']}) | {'已完成' if c['status'] == 'COMPLETED' else '进行中'} | {c['correction_count']} | {c['updated_at']} |")
        summaries = [{k: copy.deepcopy(v) for k, v in c.items() if k not in {'blocks', 'corrections'}} for c in chapters]
        return dict(book, chapters=summaries, readme='\n'.join(lines), readme_markdown='\n'.join(lines), completion_ratio=completed / len(chapters) if chapters else 0, chapter_count=len(chapters), completed_count=completed, correction_count=sum(c['correction_count'] for c in chapters))

    def notebook(self, space_id):
        self.register_plan(space_id)
        self.synchronize(space_id)
        return self._notebook(self.repository.records('notebooks', space_id=space_id)[0])

    def list_notebooks(self):
        return {'items': [self.notebook(s['id']) for s in self.spaces.repository.list()]}

    def _valid(self, evidence):
        if not (evidence and evidence.get('eligible') and evidence.get('score') is not None and not evidence.get('assisted') and not evidence.get('revoked_by_review_id') and not evidence.get('quarantined') and evidence.get('source_refs')): return False
        from knowpath_backend.learning.assessments.service import revision
        space = self.spaces.repository.get(evidence['space_id'])
        revisions = {t['id']: revision(t) for t in self.spaces.bound_topics(space)}
        return evidence.get('epoch', 0) == self.assessments._epochs(space['id']).get(evidence['topic_id'], 0) and evidence.get('topic_revision_id') == revisions.get(evidence['topic_id'])

    def synchronize(self, space_id):
        with self.repository.transaction():
            self.spaces.repository.get(space_id)
            states = self.plans._planning_states(space_id)
            nodes = self.plans._canonical_nodes(self.plans._space_plans(space_id), self.plans._space_sessions(space_id))
            tasks = {t['node_id']: t for rows in nodes.values() for t in rows}
            evidence = {e['id']: e for e in self.repository.records('evidence', space_id=space_id)}
            for c in self.repository.records('note_chapters', space_id=space_id):
                prior = copy.deepcopy(c)
                task = tasks.get(c['node_id'], {})
                if c['generation_status'] == '未总结' and any(s.get('status') == 'finished' and s.get('context', {}).get('node_id', s.get('task_id')) == c['node_id'] for s in self.plans._space_sessions(space_id)):
                    c['generation_status'] = '待总结'
                generations = self.repository.records('note_generations', chapter_id=c['id'])
                if generations:
                    latest = max(generations, key=lambda g: (g['created_at'], g['id']))
                    run = self.runs.get(latest['run_id'])
                    c['generation_status'] = '已生成' if latest['status'] == 'completed' else '失败' if latest['status'] in {'failed', 'cancelled'} or run['status'] in {'failed', 'cancelled', 'cancelling'} else '生成中' if run['status'] == 'running' else '待生成'
                    c['error'] = latest.get('error') or run.get('error')
                c['status'] = 'COMPLETED' if task.get('status') == 'completed' and c['topic_ids'] and all(states.get(t, {}).get('status') == 'mastered' and states.get(t, {}).get('score_validity') != 'stale' for t in c['topic_ids']) else 'IN_PROGRESS'
                for correction in c['corrections']:
                    candidates = [e for e in evidence.values() if e.get('assessment_id') == correction.get('assessment_id', correction['id'].split(':')[0]) and e.get('question_id') == correction['question_id'] and not e.get('revoked_by_review_id')]
                    current = max(candidates, key=lambda e: (e.get('created_at', ''), e['id']), default=None)
                    if current: correction['evidence_id'] = current['id']
                    correction['confirmed'] = self._valid(current) and current.get('result') == 'incorrect'
                    qualifying = [e for e in evidence.values() if self._valid(e) and e.get('assessment_kind') == 'retest' and e.get('result') == 'correct' and e.get('assessment_id') != correction.get('assessment_id') and e.get('topic_id') == (current or {}).get('topic_id') and e.get('created_at', '') > (current or {}).get('created_at', '')]
                    correction['independent_retest_passed'] = bool(qualifying and states.get((current or {}).get('topic_id'), {}).get('status') == 'mastered')
                    correction['retest_evidence_ids'] = [e['id'] for e in qualifying] if correction['independent_retest_passed'] else []
                for block in c['blocks']:
                    if block['kind'] != 'personal':
                        block['evidence_validity'] = 'current' if all(self._valid(evidence.get(eid)) for eid in block.get('evidence_ids', [])) else 'stale'
                c['correction_count'] = sum(x['confirmed'] for x in c['corrections'])
                if c != prior: self._save(c, 'validity')

    def _audit(self, chapter):
        from knowpath_backend.learning.materials.source_access import SourceAccessService, AssistanceDeliveryChanged
        access = SourceAccessService(self.assessments)
        refs = [r for b in chapter['blocks'] for r in b.get('source_refs', [])]
        for g in self.repository.records('note_generations', chapter_id=chapter.get('chapter_id', chapter['id'])):
            refs.extend(g.get('snapshot', {}).get('sources', []))
        if not refs: refs = chapter.get('source_refs', [])
        if not refs:
            if not chapter['blocks']: return
            # Personal prose can expose the same answers without exact locators.
            # Conservatively audit matching frozen topic identities in all spaces.
            from knowpath_backend.learning.materials.source_access import apply_source_assistance, ACTIVE_ASSESSMENTS
            topics = set(chapter['topic_ids'])
            candidates = [a for a in self.repository.records('assessments', lock=False) if a['status'] in ACTIVE_ASSESSMENTS and topics.intersection(a['topic_ids'])]
            with self.repository.transaction():
                active = [self.repository.get_record('assessments', a['id']) for a in sorted(candidates, key=lambda a: a['id'])]
                for sid in sorted({a['space_id'] for a in active} | {chapter['space_id']}): self.spaces.repository.get(sid)
                for a in active:
                    if a['status'] not in ACTIVE_ASSESSMENTS: continue
                    consulted = a['snapshot'].setdefault('consulted_topic_ids', [])
                    consulted.extend(t for t in a['topic_ids'] if t in topics and t not in consulted)
                    a['snapshot'].setdefault('assistance_events', []).append({'kind': 'note_read', 'delivery_id': uid(), 'space_id': chapter['space_id'], 'topic_ids': sorted(topics), 'source_refs': [], 'delivered_at': now()})
                    apply_source_assistance(a['questions'], a['snapshot'])
                    self.repository.put_record('assessments', a)
            return
        for _ in range(3):
            plan = access.prepare_delivery(refs, chapter['space_id'], include_bound_assessments=True)
            try:
                with self.repository.transaction():
                    active, _ = access.lock_delivery(plan, chapter['space_id'])
                    access.record_delivery(active, plan, kind='note_read', delivery_id=uid(), space_id=chapter['space_id'])
                    self._record_personal_assistance(active, chapter)
                return
            except AssistanceDeliveryChanged: continue
        raise DomainConflict('STALE_LEARNING_CONTEXT', '学习范围已变化，请重新读取笔记')

    def _record_personal_assistance(self, active, chapter):
        if not any(b['kind'] == 'personal' or b.get('edited') for b in chapter['blocks']): return
        from knowpath_backend.learning.materials.source_access import apply_source_assistance
        topics = set(chapter['topic_ids'])
        for a in active:
            overlap = sorted(topics.intersection(a['topic_ids']))
            if not overlap: continue
            consulted = a['snapshot'].setdefault('consulted_topic_ids', [])
            consulted.extend(t for t in overlap if t not in consulted)
            a['snapshot'].setdefault('assistance_events', []).append({'kind': 'note_read', 'delivery_id': uid(), 'space_id': chapter['space_id'], 'topic_ids': overlap, 'source_refs': [], 'delivered_at': now()})
            apply_source_assistance(a['questions'], a['snapshot'])
            self.repository.put_record('assessments', a)

    def chapter(self, identifier):
        c = self.repository.get_record('note_chapters', identifier)
        self.synchronize(c['space_id'])
        c, generations = self._read_delivery(identifier)
        sessions = {(b.get('learning_time') or {}).get('id'): b['learning_time'] for b in c['blocks'] if b.get('learning_time')}
        c['elapsed_seconds'] = sum(s['elapsed_seconds'] for s in sessions.values()) if sessions and all(s.get('elapsed_seconds') is not None for s in sessions.values()) else None
        c['markdown'] = '\n\n'.join(b['markdown'] for b in c['blocks'])
        return dict(c, generations=[{k: copy.deepcopy(g.get(k)) for k in ('id', 'status', 'run_id', 'error', 'snapshot')} for g in generations])

    def _read_delivery(self, identifier, revision_id=None):
        from knowpath_backend.learning.materials.source_access import SourceAccessService, AssistanceDeliveryChanged
        access = SourceAccessService(self.assessments)
        table, row_id = ('note_revisions', revision_id) if revision_id else ('note_chapters', identifier)
        def needs_topic_audit(row):
            return any(b['kind'] == 'personal' or b.get('edited') for b in row['blocks'])
        def discover():
            row = self.repository.get_record(table, row_id, lock=False)
            if revision_id and row['chapter_id'] != identifier: raise DomainNotFound('note_revision', revision_id)
            generations = self.repository.records('note_generations', chapter_id=identifier, lock=False)
            refs = list(row.get('source_refs', []))
            refs.extend(r for b in row['blocks'] for r in b.get('source_refs', []))
            refs.extend(r for g in generations for r in g.get('snapshot', {}).get('sources', []))
            if needs_topic_audit(row):
                # Resolve conservative topic-level consultation into version
                # identities before discovery so source-version locks fence
                # assessments created while this response is being assembled.
                topics = set(row['topic_ids'])
                for space in self.spaces.repository.list():
                    refs.extend(r for topic in self.spaces.bound_topics(space) if topic['id'] in topics for r in topic.get('source_refs', []))
                for assessment in self.repository.records('assessments', lock=False):
                    refs.extend(r for topic in assessment['snapshot'].get('topics', []) if topic['id'] in topics for r in topic.get('source_refs', []))
            return row, generations, refs
        for _ in range(3):
            row, generations, refs = discover()
            plan = access.prepare_delivery(refs, row['space_id'], include_bound_assessments=True)
            if needs_topic_audit(row) and not refs:
                # Truly source-less legacy topics still use the same fenced
                # response transaction, rather than a separate early audit.
                topics = set(row['topic_ids'])
                from knowpath_backend.learning.materials.source_access import ACTIVE_ASSESSMENTS
                candidates = [a for a in self.repository.records('assessments', lock=False) if a['status'] in ACTIVE_ASSESSMENTS and topics.intersection(a['topic_ids'])]
                plan['assessment_ids'] = sorted({a['id'] for a in candidates} | set(plan['assessment_ids']))
                plan['space_ids'] = sorted({s['id'] for s in self.spaces.repository.list()} | set(plan['space_ids']))
            try:
                with self.repository.transaction():
                    active, _ = access.lock_delivery(plan, row['space_id'])
                    if needs_topic_audit(row) and not refs:
                        candidates = [a for a in self.repository.records('assessments', lock=False) if a['status'] in ACTIVE_ASSESSMENTS and set(row['topic_ids']).intersection(a['topic_ids'])]
                        if {a['id'] for a in candidates} - set(plan['assessment_ids']): raise AssistanceDeliveryChanged()
                        active = [self.repository.get_record('assessments', aid) for aid in plan['assessment_ids']]
                    current = self.repository.get_record(table, row_id)
                    current_gens = self.repository.records('note_generations', chapter_id=identifier)
                    if fingerprint([current, current_gens]) != fingerprint([row, generations]): raise AssistanceDeliveryChanged()
                    access.record_delivery(active, plan, kind='note_read', delivery_id=uid(), space_id=row['space_id'])
                    self._record_personal_assistance(active, current)
                    return copy.deepcopy(current), copy.deepcopy(current_gens)
            except AssistanceDeliveryChanged: continue
        raise DomainConflict('STALE_LEARNING_CONTEXT', '笔记在读取期间更新，请重试')

    def edit(self, identifier, payload, key=None):
        self._edit_command(identifier, payload, key)
        # Audit and assemble after committing the edit. A generation committed
        # concurrently is included only under the same fenced read protocol.
        return self.chapter(identifier)

    def _edit_command(self, identifier, payload, key=None):
        fp = fingerprint([identifier, payload])
        with self.repository.transaction():
            if key:
                replay = self.repository.replay(key, fp)
                if replay is not None: return replay
            c = self.repository.get_record('note_chapters', identifier)
            if payload.get('expected_version') != c['version']: raise DomainConflict('VERSION_CONFLICT', '笔记已更新，请刷新后重试', {'latest_version': c['version']})
            old = {b['id']: b for b in c['blocks']}
            if 'blocks' in payload:
                for item in payload['blocks']:
                    if not isinstance(item.get('markdown'), str): raise DomainConflict('INVALID_NOTE', '正文必须为 Markdown 文本')
                    if item.get('id') in old:
                        old[item['id']]['markdown'] = item['markdown']
                        old[item['id']]['edited'] = True
                    else:
                        block = {'id': uid(), 'kind': 'personal', 'markdown': item['markdown'], 'source_refs': [], 'evidence_ids': [], 'created_at': now()}
                        c['blocks'].append(block)
            corrections = {x['id']: x for x in c['corrections']}
            for item in payload.get('corrections', []):
                if item.get('id') not in corrections: raise DomainConflict('INVALID_CORRECTION', '纠偏记录不存在')
                corrections[item['id']]['markdown'] = str(item['markdown'])
                corrections[item['id']]['edited'] = True
            result = self._save(c, 'edit')
            if key: self.repository.remember(key, fp, identifier, {'chapter_id': identifier})
            return result

    def revisions(self, identifier):
        self.repository.get_record('note_chapters', identifier)
        return [{k: row.get(k) for k in ('id', 'chapter_id', 'version', 'updated_at', 'reason')} for row in self.repository.records('note_revisions', chapter_id=identifier)]

    def revision(self, identifier, revision_id):
        row, _ = self._read_delivery(identifier, revision_id)
        return row

    def enqueue_assessment(self, assessment):
        if assessment['status'] != 'completed' or not assessment.get('task_id'): return None
        with self.repository.transaction():
            existing = self.repository.records('note_generations', assessment_id=assessment['id'])
            if existing: return self.assessment_reference(assessment['id'])
            self.register_plan(assessment['space_id'])
            plans = self.plans._space_plans(assessment['space_id'])
            task = next((t for p in plans for t in p['tasks'] if t['id'] == assessment['task_id']), None)
            if not task: return None
            node = task.get('node_id', task.get('context', {}).get('node_id', task['id']))
            c = self.repository.records('note_chapters', space_id=assessment['space_id'], node_id=node)[0]
            evidence = [e for e in self.repository.records('evidence', assessment_id=assessment['id']) if self._valid(e)]
            source_refs = [r for e in evidence for r in e.get('source_refs', [])]
            sources = []
            for ref in source_refs:
                version = self.spaces.materials.get_version(ref['material_version_id'])
                chunk = next((x for x in version.chunks if x.id == ref['chunk_id']), None) if version and version.material_id == ref['material_id'] else None
                if chunk: sources.append(dict(ref, text=chunk.text))
            allowed = {(r['material_id'], r['material_version_id'], r['chunk_id']) for r in sources}
            evidence = [e for e in evidence if e['source_refs'] and all((r['material_id'], r['material_version_id'], r['chunk_id']) in allowed for r in e['source_refs'])]
            session = next((s for s in self.plans._space_sessions(c['space_id']) if s['id'] == assessment.get('learning_session_id')), None)
            run = self.runs.create('note_generate')
            g = {'id': uid(), 'space_id': c['space_id'], 'chapter_id': c['id'], 'assessment_id': assessment['id'], 'run_id': run['id'], 'status': 'generating', 'fingerprint': fingerprint(assessment['result']), 'snapshot': {'assessment_result': copy.deepcopy(assessment['result']), 'questions': [q for q in assessment['questions'] if q['id'] in {e['question_id'] for e in evidence}], 'evidence': evidence, 'sources': sources, 'learning_time': copy.deepcopy(session), 'topic_ids': c['topic_ids']}, 'created_at': now(), 'error': None}
            g['event_id'] = enqueue(self.repository, 'note', g)
            self._put('note_generations', g)
            c.update(generation_status='待生成', generation_id=g['id'], run_id=run['id'])
            self._save(c, 'draft')
            return self.assessment_reference(assessment['id'])

    def assessment_reference(self, identifier):
        rows = self.repository.records('note_generations', assessment_id=identifier)
        return {'chapter_id': rows[0]['chapter_id'], 'generation_id': rows[0]['id'], 'run_id': rows[0]['run_id'], 'status': rows[0]['status']} if rows else None

    def chapter_reference(self, space_id, node_id):
        rows = self.repository.records('note_chapters', space_id=space_id, node_id=node_id)
        return {'chapter_id': rows[0]['id'], 'status': rows[0]['status'], 'generation_status': rows[0]['generation_status']} if rows else None

    def retry(self, identifier, key=None):
        discovered = self.repository.get_record('note_generations', identifier, lock=False)
        with self.repository.transaction():
            assessment = self.repository.get_record('assessments', discovered['assessment_id'])
            self.spaces.repository.get(discovered['space_id'])
            fp = fingerprint(['note.retry', identifier])
            if key:
                replay = self.repository.replay(key, fp)
                if replay is not None: return replay
            g = self.repository.get_record('note_generations', identifier)
            if g['status'] == 'generating': return {'generation_id': identifier, 'run_id': g['run_id']}
            if g['status'] != 'failed': raise DomainConflict('NOTE_NOT_RETRYABLE', '该笔记任务不可重试')
            evidence = [e for e in self.repository.records('evidence', assessment_id=assessment['id']) if self._valid(e)]
            g['fingerprint'] = fingerprint(assessment['result'])
            g['snapshot']['assessment_result'] = copy.deepcopy(assessment['result'])
            g['snapshot']['evidence'] = evidence
            g['snapshot']['questions'] = [q for q in assessment['questions'] if q['id'] in {e['question_id'] for e in evidence}]
            run = self.runs.create('note_generate')
            g.update(status='generating', run_id=run['id'], error=None)
            g['event_id'] = enqueue(self.repository, 'note', g)
            self._put('note_generations', g)
            c = self.repository.get_record('note_chapters', g['chapter_id'])
            c.update(generation_status='待生成', run_id=run['id'])
            self._save(c, 'retry')
            result = {'generation_id': identifier, 'run_id': run['id']}
            if key: self.repository.remember(key, fp, identifier, result)
            return result

    def generate(self, identifier, *, job=None):
        try: g = self.repository.get_record('note_generations', identifier)
        except DomainNotFound: return
        if g['status'] != 'generating': return
        snapshot = copy.deepcopy(g['snapshot'])
        error, raw = None, None
        try:
            if self.generator is None: raise RuntimeError('MODEL_UNAVAILABLE')
            if hasattr(self.generator, 'generate_json'):
                raw = self.generator.generate_json([{'role': 'system', 'content': '仅整理已验证资料，禁止推断掌握状态。返回JSON {"items":[{"evidence_id":"输入中的证据ID","markdown":"核心知识总结"}],"corrections":[{"evidence_id":"输入中incorrect证据ID","markdown":"理解纠偏说明"}]}。每项必须对应输入证据，不加入未学习知识。'}, {'role': 'user', 'content': json.dumps(snapshot, ensure_ascii=False)}])
            else: raw = self.generator.generate(snapshot)
            if not isinstance(raw, dict) or not isinstance(raw.get('items'), list) or not isinstance(raw.get('corrections', []), list): raise ValueError('MODEL_INVALID_RESPONSE')
            valid = {e['id']: e for e in snapshot['evidence']}
            for item in raw['items'] + raw.get('corrections', []):
                if item.get('evidence_id') not in valid or not isinstance(item.get('markdown'), str): raise ValueError('MODEL_INVALID_RESPONSE')
            for item in raw.get('corrections', []):
                if valid[item['evidence_id']]['result'] != 'incorrect': raise ValueError('MODEL_INVALID_RESPONSE')
            raw['items'] = list({item['evidence_id']: item for item in reversed(raw['items'])}.values())
            raw['corrections'] = list({item['evidence_id']: item for item in reversed(raw.get('corrections', []))}.values())
        except Exception as exc:
            code = getattr(exc, 'code', str(exc) if str(exc) in {'MODEL_INVALID_RESPONSE', 'MODEL_UNAVAILABLE'} else 'MODEL_UNAVAILABLE')
            error = {'code': code, 'message': '笔记生成失败，结构化草稿已保留', 'details': {}, 'retryable': code != 'MODEL_INVALID_RESPONSE'}
        with self.repository.transaction():
            try:
                assessment = self.repository.get_record('assessments', g['assessment_id'])
                self.spaces.repository.get(g['space_id'])
                current = self.repository.get_record('note_generations', identifier)
                c = self.repository.get_record('note_chapters', g['chapter_id'])
            except DomainNotFound: return
            if job is not None: job.check()
            if current['run_id'] != g['run_id'] or current['status'] != 'generating': return
            run = self.runs.get(g['run_id'])
            if run['status'] in {'cancelled', 'cancelling'}:
                if run['status'] == 'cancelling': self.runs.acknowledge_cancel(run['id'])
                current['status'] = 'cancelled'
                c['generation_status'] = '失败'
            elif run['status'] != 'running': return
            else:
                if fingerprint(assessment['result']) != current['fingerprint']: error = {'code': 'STALE_INPUT', 'message': '测评结果已修订，请重新生成', 'details': {}, 'retryable': True}
                for source in snapshot['sources']:
                    version = self.spaces.materials.get_version(source['material_version_id'])
                    if not version or not any(x.id == source['chunk_id'] and x.text == source['text'] for x in version.chunks): error = {'code': 'STALE_INPUT', 'message': '资料来源已失效', 'details': {}, 'retryable': False}
                if error:
                    if job is not None and job.retry(error): return
                    current.update(status='failed', error=error)
                    c['generation_status'] = '失败'
                    self.runs.fail(run['id'], error)
                else:
                    existing_evidence = {e['id']: e for e in self.repository.records('evidence', assessment_id=assessment['id'])}
                    items = [x for x in raw['items'] if self._valid(existing_evidence.get(x['evidence_id']))]
                    valid_corrections = [x for x in raw.get('corrections', []) if self._valid(existing_evidence.get(x['evidence_id'])) and existing_evidence[x['evidence_id']]['result'] == 'incorrect']
                    refs = [r for x in items + valid_corrections for r in existing_evidence.get(x['evidence_id'], {}).get('source_refs', [])]
                    # Result facts are also sourced; scrubbing must remove the
                    # block if any displayed question dependency is deleted.
                    refs.extend(r for x in snapshot['assessment_result'].get('question_results', []) for r in x.get('source_refs', []))
                    elapsed = (snapshot['learning_time'] or {}).get('elapsed_seconds')
                    time_text = f'{elapsed} 秒（包含暂停）' if elapsed is not None else '未记录'
                    result_rows = snapshot['assessment_result'].get('question_results', [])
                    score_text = '\n'.join(f"- {x['question_id']}：{x['verdict']}；分数 {x.get('score') if x.get('score') is not None else '未验证'}" for x in result_rows)
                    source_text = '\n'.join(f"- [{r['chunk_id']}](/api/v1/materials/{r['material_id']}/versions/{r['material_version_id']}/chunks/{r['chunk_id']}?space_id={c['space_id']})" for r in refs)
                    core = '\n\n'.join(x['markdown'] for x in items) or '本次暂无独立验证的核心知识。'
                    corrections_text = '\n'.join('- 纠偏记录：' + assessment['id'] + ':' + existing_evidence[x['evidence_id']]['question_id'] for x in valid_corrections) or '本次暂无已确认纠偏。'
                    pending = '\n'.join('- ' + x['question_id'] for x in result_rows if x['verdict'] != 'correct') or '以当前掌握状态和后续独立复测为准。'
                    markdown = f"## 学习范围\n{c['title']}\n\n## 核心知识\n{core}\n\n## 本次学习时间\n{time_text}\n\n## 测评结果\n{score_text}\n\n## 理解纠偏\n{corrections_text}\n\n## 待巩固内容\n{pending}\n\n## 验收情况\n章节完成状态由任务与当前版本全部主题的后端掌握状态确定。\n\n## 来源\n{source_text}"
                    core_items = [dict(x, question_id=existing_evidence[x['evidence_id']]['question_id'], source_refs=existing_evidence[x['evidence_id']]['source_refs']) for x in items]
                    block = {'id': identifier, 'kind': 'assessment', 'assessment_id': assessment['id'], 'markdown': markdown, 'items': core_items, 'source_refs': refs, 'evidence_ids': [x['evidence_id'] for x in items], 'facts': snapshot['assessment_result'], 'learning_time': snapshot['learning_time'], 'timing_note': '会话计时包含暂停；缺失时间显示未记录', 'created_at': now()}
                    if not any(b['id'] == identifier for b in c['blocks']): c['blocks'].append(block)
                    known = {x['id'] for x in c['corrections']}
                    for x in raw.get('corrections', []):
                        e = existing_evidence.get(x['evidence_id'])
                        if not self._valid(e): continue
                        cid = assessment['id'] + ':' + e['question_id']
                        if cid not in known:
                            c['corrections'].append({'id': cid, 'assessment_id': assessment['id'], 'markdown': x['markdown'], 'evidence_id': e['id'], 'question_id': e['question_id'], 'source_refs': e['source_refs'], 'confirmed': True})
                            known.add(cid)
                    current.update(status='completed', error=None)
                    c['generation_status'] = '已生成'
                    self.runs.complete(run['id'], {'type': 'note_chapter', 'id': c['id']})
            self._put('note_generations', current)
            self._save(c, 'generation')
            self.synchronize(c['space_id'])
            if job is not None: job.settle()

    def _delete(self, table, identifier):
        if hasattr(self.repository, 'unit_of_work'):
            from knowpath_backend.learning.persistence.learning_repository import TABLES
            with self.repository.unit_of_work.session() as session:
                row = session.get(TABLES[table], identifier)
                if row: session.delete(row); session.flush()
        else: self.repository.materials.assessment_data[table].pop(identifier, None)

    def delete_material(self, material_id):
        def depends(value):
            if isinstance(value, dict): return value.get('material_id') == material_id or any(depends(v) for v in value.values())
            if isinstance(value, list): return any(depends(v) for v in value)
            return False
        with self.repository.transaction():
            affected = set()
            for table in ('note_chapters', 'note_revisions'):
                for c in self.repository.records(table):
                    before = copy.deepcopy(c)
                    c['blocks'] = [b for b in c['blocks'] if b['kind'] == 'personal' or not any(r['material_id'] == material_id for r in b.get('source_refs', []))]
                    c['corrections'] = [x for x in c['corrections'] if not any(r['material_id'] == material_id for r in x.get('source_refs', []))]
                    c['source_refs'] = [r for r in c['source_refs'] if r['material_id'] != material_id]
                    c['correction_count'] = sum(x['confirmed'] for x in c['corrections'])
                    if c != before:
                        c['status'] = 'IN_PROGRESS'
                        if table == 'note_chapters':
                            c['version'] += 1
                            c['updated_at'] = now()
                        self._put(table, c)
                        affected.add(c.get('chapter_id', c['id']))
            for g in self.repository.records('note_generations'):
                if depends(g.get('snapshot', {})):
                    if self.runs.get(g['run_id'])['status'] in {'queued', 'running'}:
                        self.runs.request_cancel(g['run_id'])
                        self.runs.acknowledge_cancel(g['run_id'])
                    g.update(status='cancelled', snapshot={}, fingerprint='deleted')
                    self._put('note_generations', g)
                    for event in self.repository.records('outbox', aggregate_id=g['id']):
                        event.update(status='cancelled', lease_token=None, lease_until=None)
                        self._put('outbox', event)

    def delete_space(self, space_id):
        with self.repository.transaction():
            chapters = self.repository.records('note_chapters', space_id=space_id)
            for g in self.repository.records('note_generations', space_id=space_id):
                if self.runs.get(g['run_id'])['status'] in {'queued', 'running'}:
                    self.runs.request_cancel(g['run_id'])
                    self.runs.acknowledge_cancel(g['run_id'])
                self._delete('note_generations', g['id'])
            for c in chapters:
                for r in self.repository.records('note_revisions', chapter_id=c['id']): self._delete('note_revisions', r['id'])
                self._delete('note_chapters', c['id'])
            for book in self.repository.records('notebooks', space_id=space_id): self._delete('notebooks', book['id'])
