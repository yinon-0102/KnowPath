"""Transactional material erasure and a durable, idempotent external cleanup job."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, select
from .assessments import revision
from knowpath_backend.learning.persistence.db import ExportRow, IdempotencyRow
from .errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.persistence.learning_repository import TABLES
from .mastery import aggregate
from .material_schemas import DeleteMaterial
from .model_tasks import EVENTS as MODEL_EVENTS
from .spaces import SpaceService, now


def references(value, material_id):
    if isinstance(value, dict):
        return value.get('material_id') == material_id or any(references(v, material_id) for v in value.values())
    if isinstance(value, list):
        return any(references(v, material_id) for v in value)
    return False


class MaterialDeletionService:
    def __init__(self, repository, spaces, graphs, runs):
        self.repository, self.spaces, self.graphs, self.runs = repository, spaces, graphs, runs
        self.materials = spaces.materials
        self.uow = getattr(repository, 'unit_of_work', None)
        self.commands = SpaceService(repository, self.materials)

    def _remove(self, table, identifiers):
        if not identifiers:
            return
        if self.uow:
            with self.uow.session() as session:
                session.execute(delete(TABLES[table]).where(TABLES[table].id.in_(identifiers)))
        else:
            for identifier in identifiers:
                self.materials.assessment_data[table].pop(identifier, None)

    def _cancel(self, run_id):
        if not run_id:
            return
        try:
            self.runs.request_cancel(run_id)
            self.runs.acknowledge_cancel(run_id)
        except DomainNotFound:
            pass

    def _cancel_generation(self, resource):
        # Keep the worker's owner -> outbox -> Run lock order. Revoking the token
        # in this transaction prevents any already-running model from publishing.
        for event in self.repository.records('outbox', aggregate_id=resource['id']):
            if event['event_type'] in MODEL_EVENTS and event['status'] in {'pending', 'processing'}:
                event.update(status='cancelled', lease_token=None, lease_until=None, available_at=None)
                self.repository.put_record('outbox', event)
        self._cancel(resource.get('run_id'))

    def _space(self, space, material_id, assessments):
        affected = {t['id'] for t in self.spaces.bound_topics(space) if references(t, material_id)}
        question_ids = set()
        for assessment in assessments:
            if assessment['space_id'] != space['id']:
                continue
            removed = [q for q in assessment['questions'] if references(q, material_id)]
            if not removed and not references(assessment['snapshot'], material_id):
                continue
            question_ids.update(q['id'] for q in removed)
            affected.update(q['topic_id'] for q in removed)
            snapshot = assessment['snapshot']
            snapshot['bindings'] = [b for b in snapshot['bindings'] if b['material_id'] != material_id]
            snapshot['topics'] = [t for t in snapshot['topics'] if not references(t, material_id)]
            assessment['questions'] = [q for q in assessment['questions'] if q['id'] not in question_ids]
            assessment['topic_ids'] = [t for t in assessment['topic_ids'] if t not in affected]
            if assessment['status'] == 'generating' or not assessment['questions']:
                assessment.update(status='stale', result=None)
                self._cancel_generation(assessment)
            elif removed and assessment.get('result'):
                result = assessment['result']
                result['question_results'] = [q for q in result['question_results'] if q['question_id'] not in question_ids]
                kept_topics = {q['topic_id'] for q in assessment['questions']}
                result['topic_results'] = [t for t in result['topic_results'] if t['topic_id'] in kept_topics]
            original = snapshot.get('original_graded_result')
            if original:
                original['question_results'] = [q for q in original['question_results'] if q['question_id'] not in question_ids]
                original['topic_results'] = [t for t in original['topic_results'] if t['topic_id'] not in affected]
            if 'grade_reviews' in assessment:
                for review in assessment['grade_reviews']:
                    if 'topic_results' in review:
                        review['topic_results'] = [t for t in review['topic_results'] if t['topic_id'] not in affected]
                assessment['grade_reviews'] = [r for r in assessment['grade_reviews'] if r['question_id'] not in question_ids]
            self.repository.put_record('assessments', assessment)
            self._remove('attempts', [a['id'] for a in self.repository.records('attempts', assessment_id=assessment['id']) if a['question_id'] in question_ids])
        # A queued answer freezes source text and conversation context. Remove
        # references to erased material and terminate its generation in the same
        # transaction as the binding change; an old model result is now fenced.
        for message in self.repository.records('messages', space_id=space['id']):
            if not references(message['snapshot'], material_id):
                continue
            snapshot = message['snapshot']
            snapshot['sources'] = [s for s in snapshot['sources'] if not references(s, material_id)]
            if 'retrieval_sources' in snapshot:
                snapshot['retrieval_sources'] = [s for s in snapshot['retrieval_sources'] if not references(s, material_id)]
            snapshot['bindings'] = [b for b in snapshot['bindings'] if not references(b, material_id)]
            snapshot['history'] = []
            snapshot.pop('memory', None)
            snapshot.pop('context_report', None)
            if message['status'] in {'pending', 'generating'}:
                self._cancel_generation(message)
                message.update(status='cancelled', response=None)
            self.repository.put_record('messages', message)
        evidence = self.repository.records('evidence', space_id=space['id'])
        removed = [e for e in evidence if references(e, material_id) or e.get('question_id') in question_ids]
        affected.update(e['topic_id'] for e in removed)
        self._remove('evidence', [e['id'] for e in removed])
        space.update(bindings=[b for b in space['bindings'] if b['material_id'] != material_id],
                     space_version=space['space_version'] + 1, scope_version=space['scope_version'] + 1,
                     state_version=space['state_version'] + 1, updated_at=now())
        # Keep explicit selections: removing a selected source must never expand scope.
        current = {t['id']: t for t in self.spaces.bound_topics(space)}
        remaining = [e for e in evidence if e not in removed]
        for previous in self.repository.records('states', space_id=space['id']):
            if previous['topic_id'] not in affected:
                continue
            tid = previous['topic_id']
            rid = revision(current[tid]) if tid in current else previous.get('topic_revision_id', '')
            relevant = [e for e in remaining if e['topic_id'] == tid and e.get('epoch', 0) == previous.get('epoch', 0)
                        and e.get('topic_revision_id') == rid and not e.get('revoked_by_review_id')]
            value = aggregate(tid, rid, relevant, {}, space['state_version'],
                              max((e['created_at'] for e in relevant), default=None))
            value.update(id=previous['id'], space_id=space['id'], epoch=previous.get('epoch', 0))
            self.repository.put_record('states', value)
        for plan in self.repository.records('plans', space_id=space['id']):
            if plan['status'] != 'superseded':
                plan.update(status='needs_replan', version=plan['version'] + 1)
                plan.setdefault('config', {})['invalidated_topic_ids'] = sorted(affected | set(plan.get('config', {}).get('invalidated_topic_ids', [])))
                self.repository.put_record('plans', plan)
        self.spaces.repository.put(space)

    def _invalidate_replays(self, material_id, space_ids):
        if self.uow:
            with self.uow.session() as session:
                session.execute(delete(ExportRow).where(ExportRow.space_id.in_(space_ids)))
                for row in session.scalars(select(IdempotencyRow).with_for_update()):
                    if row.resource_id == material_id or references(row.response, material_id):
                        row.response = {'resource_deleted': True}
        else:
            self.materials.export_data = {k: v for k, v in self.materials.export_data.items() if v['space_id'] not in space_ids}
            for key, record in self.materials.idempotency.items():
                if record[1] == material_id or references(self.materials.idempotency_responses.get(key), material_id):
                    self.materials.idempotency_responses[key] = {'resource_deleted': True}

    def delete(self, material_id, payload):
        payload = DeleteMaterial.model_validate(payload).model_dump()
        def change():
            # Consistent with assessment finalize and space deletion: assessment -> space -> material.
            assessments = self.repository.records('assessments')
            spaces = self.repository.list()
            material = self.graphs._material(material_id)
            if material.version != payload['expected_version']:
                raise DomainConflict('VERSION_CONFLICT', '资料版本已变化')
            impacted = [s for s in spaces if references(s['bindings'], material_id)]
            if impacted and not payload['cascade']:
                raise DomainConflict('MATERIAL_IN_USE', '资料仍被学习空间引用',
                    {'impacted_spaces': [{'id': s['id'], 'name': s['name']} for s in impacted]})
            histories = self.graphs._history(material_id)
            affected_spaces = [s for s in spaces if s in impacted or any(a['space_id'] == s['id'] and references(a, material_id) for a in assessments)
                               or any(references(e, material_id) for e in self.repository.records('evidence', space_id=s['id']))
                               or any(references(m['snapshot'], material_id) for m in self.repository.records('messages', space_id=s['id']))]
            for space in affected_spaces:
                self._space(space, material_id, assessments)
            collections = {(r.get('preparation') or {}).get('qdrant', {}).get('collection') for r in histories}
            collections.discard(None)
            for event in self.repository.records('outbox'):
                if event['event_type'] != 'material.delete' and references(event['payload'], material_id):
                    collections.update(event['payload'].get('external_collections', []))
                    self._cancel(event['payload'].get('run_id'))
            for row in histories:
                self._cancel(row.get('run_id'))
            corrections = [c for c in self.repository.records('corrections') if references(c, material_id)]
            self._remove('corrections', [c['id'] for c in corrections])
            self.graphs.repository.delete_history(material_id, [r['id'] for r in histories])
            self._invalidate_replays(material_id, {s['id'] for s in affected_spaces})
            self.materials.delete_material(material_id)
            run = self.runs.create('material_delete')
            event = {'id': str(uuid4()), 'event_type': 'material.delete', 'aggregate_type': 'material',
                     'aggregate_id': material_id, 'payload': {'material_id': material_id, 'run_id': run['id'],
                         'revision_ids': [r['id'] for r in histories], 'collections': sorted(collections)}, 'status': 'pending', 'attempts': 0,
                     'lease_token': None, 'lease_until': None, 'available_at': None, 'created_at': now()}
            self.repository.put_record('outbox', event)
            return {'run_id': run['id'], 'material_id': material_id, 'status': 'queued'}
        return self.commands._execute('material.delete', material_id, payload, None, change)


class MaterialDeletionWorker:
    """Retry forever with bounded backoff; a deleted SQL row is not cleanup success."""
    def __init__(self, service, cleaner, *, clock=None, lease_seconds=300):
        if lease_seconds < 1:
            raise ValueError('positive lease required')
        self.service, self.repository, self.cleaner = service, service.repository, cleaner
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds

    def _run(self, event):
        # The outbox still owns external erasure if its progress Run was removed.
        run_id = event['payload']['run_id']
        try:
            return self.service.runs.get(run_id)
        except DomainNotFound as exc:
            if exc.resource != 'run' or exc.resource_id != run_id:
                raise
            return None

    def claim(self, event_id):
        with self.repository.transaction():
            event = self.repository.get_record('outbox', event_id)
            if event['event_type'] != 'material.delete' or event['status'] not in {'pending', 'processing'}:
                return None
            timestamp = self.clock()
            if any(event.get(k) and datetime.fromisoformat(event[k]) > timestamp for k in ('lease_until', 'available_at')):
                return None
            event.update(status='processing', attempts=event['attempts'] + 1, available_at=None,
                         lease_token=str(uuid4()), lease_until=(timestamp + timedelta(seconds=self.lease_seconds)).isoformat())
            self.repository.put_record('outbox', event)
            run = self._run(event)
            if run is not None and run['status'] == 'queued':
                self.service.runs.start(event['payload']['run_id'])
            return copy.deepcopy(event)

    def execute(self, claimed):
        succeeded = False
        try:
            self.cleaner.delete(claimed['payload'])
            succeeded = True
        except Exception:
            pass  # Persist retry metadata, never credentials or source content.
        with self.repository.transaction():
            event = self.repository.get_record('outbox', claimed['id'])
            if event['status'] != 'processing' or event['lease_token'] != claimed['lease_token']:
                return False
            event.update(status='completed' if succeeded else 'pending', lease_token=None, lease_until=None,
                         available_at=None if succeeded else (self.clock() + timedelta(seconds=min(300, 2 ** min(event['attempts'], 8)))).isoformat())
            self.repository.put_record('outbox', event)
            run = self._run(event)
            if run is not None and run['status'] == 'cancelling':
                self.service.runs.acknowledge_cancel(run['id'])
            elif succeeded and run is not None and run['status'] == 'running':
                self.service.runs.complete(run['id'], {'type': 'material', 'id': event['aggregate_id'], 'deleted': True})
        return succeeded

    def run_once(self, event_id=None):
        candidates = [{'id': event_id}] if event_id else self.repository.records('outbox', lock=False, event_type='material.delete')
        for candidate in candidates:
            claimed = self.claim(candidate['id'])
            if claimed:
                self.execute(claimed)
                return True
        return False


class ExternalMaterialCleaner:
    """Only material-filtered points and exact revision IDs are erased."""
    def __init__(self, preparer):
        self.graph, self.vectors = preparer.graph, preparer.vectors.backend

    def delete(self, payload):
        from neo4j import Query
        from qdrant_client import models
        identifiers = payload['revision_ids']
        if identifiers:
            with self.graph.driver.session(database=self.graph.database) as session:
                session.run(Query('MATCH (n) WHERE (n:KPRevision AND n.id IN $ids) OR '
                    '((n:KPTopicVariant OR n:KPSource) AND n.kp_revision_id IN $ids) DETACH DELETE n', timeout=60), ids=identifiers).consume()
                count = session.run(Query('MATCH (n) WHERE (n:KPRevision AND n.id IN $ids) OR '
                    '((n:KPTopicVariant OR n:KPSource) AND n.kp_revision_id IN $ids) RETURN count(n) AS count', timeout=60), ids=identifiers).single()['count']
                if count:
                    raise RuntimeError('graph cleanup not verified')
        # Include historic embedding profiles and points left by partial preparations.
        prefix = self.vectors.collection.rsplit('_', 1)[0]
        selector = models.Filter(must=[models.FieldCondition(key='material_id', match=models.MatchValue(value=payload['material_id']))])
        for item in self.vectors.client.get_collections().collections:
            if item.name not in payload.get('collections', []) and not re.fullmatch(re.escape(prefix) + r'_[a-f0-9]{16}', item.name):
                continue
            self.vectors.client.delete(item.name, points_selector=models.FilterSelector(filter=selector), wait=True)
            if self.vectors.client.count(item.name, count_filter=selector, exact=True).count:
                raise RuntimeError('vector cleanup not verified')
