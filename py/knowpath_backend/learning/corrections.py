"""Append-only correction candidates and explicit, durable publication requests."""
import copy
from uuid import uuid4
from .correction_schemas import CreateCorrection, ConfirmCorrection
from .errors import DomainConflict, DomainNotFound
from .graph_reconciliation import compare_snapshots, digest
from .spaces import SpaceService, now


def validate_relations(snapshot):
    nodes = {n['id']: n for n in snapshot['nodes']}
    prerequisites = {identifier: set() for identifier in nodes}
    for edge in snapshot['relations']:
        if edge.get('status') in {'rejected', 'superseded', 'inactive'}:
            continue
        if edge['from_id'] not in nodes or edge['to_id'] not in nodes:
            raise DomainConflict('INVALID_RELATION', '关系端点必须属于同一快照')
        if any(nodes[edge[k]].get('status') == 'rejected' for k in ('from_id', 'to_id')):
            raise DomainConflict('INVALID_RELATION', '关系不能引用已拒绝的主题')
        if edge['type'] == 'prerequisite_of' and edge.get('status') == 'active' and edge.get('source_refs'):
            prerequisites[edge['to_id']].add(edge['from_id'])
    visited, visiting = set(), set()
    def visit(identifier):
        if identifier in visiting:
            raise DomainConflict('PREREQUISITE_CYCLE', '前置关系不能形成环')
        if identifier not in visited:
            visiting.add(identifier)
            for parent in prerequisites[identifier]:
                visit(parent)
            visiting.remove(identifier)
            visited.add(identifier)
    for identifier in nodes:
        visit(identifier)
        nodes[identifier]['prerequisites'] = sorted(prerequisites[identifier])


class CorrectionService:
    def __init__(self, graph, spaces):
        self.graph, self.spaces, self.repository = graph, spaces, graph.repository
        self.commands = SpaceService(self.repository, graph.materials)

    def create(self, space_id, payload, key=None):
        payload = CreateCorrection.model_validate(payload).model_dump()
        def change():
            space = self.spaces.repository.get(space_id)
            field = 'nodes' if payload['kind'] == 'node' else 'relations'
            selected = None
            # Space -> sorted materials -> revisions is also the worker lock order.
            for binding in sorted(space['bindings'], key=lambda b: b['material_id']):
                self.graph._material(binding['material_id'])
                if not binding.get('graph_revision_id'):
                    continue
                revision = self.repository.get_record('graph_revisions', binding['graph_revision_id'])
                if (revision['material_id'] != binding['material_id'] or revision['graph_version'] != binding['graph_version']
                        or revision['material_version_id'] != binding['material_version_id'] or not revision.get('publication')):
                    raise DomainConflict('BOUND_VERSION_UNAVAILABLE', '空间绑定的图谱不可用')
                snapshot = revision['publication']['snapshot']
                target = next((n for n in snapshot[field] if n['id'] == payload['target_id']), None)
                if target is not None:
                    selected = revision, target
                    break
            if selected is None:
                raise DomainNotFound(payload['kind'], payload['target_id'])
            base, target = selected
            history = self.graph._history(base['material_id'])
            latest = self.graph._published(history)
            if latest is None or latest['id'] != base['id']:
                raise DomainConflict('VERSION_CONFLICT', '请先采用最新发布快照后再提交纠错')
            if target.get('status') == 'rejected':
                raise DomainConflict('TARGET_REJECTED', '目标已经被拒绝')
            source_ids = {s['id'] for s in base['publication']['snapshot']['sources']}
            if payload['source_ref'] not in source_ids:
                raise DomainNotFound('source_chunk', payload['source_ref'])
            candidate = copy.deepcopy(base['publication']['snapshot'])
            updated = next(n for n in candidate[field] if n['id'] == payload['target_id'])
            if payload['action'] == 'reject':
                updated['status'] = 'rejected'
                if payload['kind'] == 'node':
                    updated['automatic_questions'] = False
                    for edge in candidate['relations']:
                        if payload['target_id'] in (edge['from_id'], edge['to_id']):
                            edge['status'] = 'rejected'
            else:
                updated.update(payload['proposed_value'])
            if updated == target:
                raise DomainConflict('CORRECTION_NO_CHANGE', '纠错没有改变目标')
            validate_relations(candidate)
            difference = compare_snapshots(base['publication']['snapshot'], candidate)
            affected = set(difference['affected_topic_ids'])
            for item in difference['changed']:
                if item['kind'] == 'relation':
                    for side in ('before', 'after'):
                        affected.update(item[side][k] for k in ('from_id', 'to_id'))
            difference['affected_topic_ids'] = sorted(affected)
            identifier, revision_id, timestamp = str(uuid4()), str(uuid4()), now()
            difference['correction_id'] = identifier
            run = self.graph.runs.create('knowledge_publish')
            revision = {'id': revision_id, 'material_id': base['material_id'], 'material_version_id': base['material_version_id'],
                'sequence': max(r['sequence'] for r in history)+1, 'base_graph_version': base['graph_version'],
                'graph_version': None, 'status': 'draft', 'run_id': run['id'], 'snapshot': candidate,
                'snapshot_hash': digest(candidate), 'diff': difference, 'created_at': timestamp}
            self.repository.put_record('graph_revisions', revision)
            correction = {'id': identifier, 'space_id': space_id, **payload, 'status': 'pending', 'created_at': timestamp,
                'material_id': base['material_id'], 'candidate_revision_id': revision_id, 'base_graph_version': base['graph_version'],
                'run_id': run['id'], 'confirmation': None}
            self.repository.put_record('corrections', correction)
            return {'correction_id': identifier, 'status': 'pending', 'candidate_revision_id': revision_id}
        return self.commands._execute('correction.create', space_id, payload, key, change)

    def confirm(self, space_id, correction_id, payload, key=None):
        payload = ConfirmCorrection.model_validate(payload).model_dump()
        def change():
            self.spaces.repository.get(space_id)
            peek = self.repository.get_record('corrections', correction_id, lock=False)
            if peek['space_id'] != space_id:
                raise DomainNotFound('correction', correction_id)
            if not peek.get('candidate_revision_id') or not peek.get('material_id'):
                raise DomainConflict('CORRECTION_NOT_PENDING', '旧纠错缺少来源快照，请重新提交')
            self.graph._material(peek['material_id'])
            revision = self.repository.get_record('graph_revisions', peek['candidate_revision_id'])
            correction = self.repository.get_record('corrections', correction_id)
            if correction['confirmation']:
                if correction['confirmation']['request'] != payload:
                    raise DomainConflict('CORRECTION_ALREADY_CONFIRMED', '纠错已按其他确认内容提交')
                return {'run_id': correction['run_id'], 'status': 'queued', 'candidate_revision_id': revision['id']}
            current = self.graph._published(self.graph._history(correction['material_id']))
            if current is None or current['graph_version'] != payload['expected_graph_version'] or revision['base_graph_version'] != current['graph_version']:
                raise DomainConflict('VERSION_CONFLICT', '图谱版本已变化，请重新提交纠错')
            if self.graph.runs.get(correction['run_id'])['status'] != 'queued':
                raise DomainConflict('CORRECTION_NOT_PENDING', '纠错任务已结束，请重新提交')
            correction.update(status='confirming', confirmation={'request': payload, 'confirmed_at': now()})
            self.repository.put_record('corrections', correction)
            self.repository.put_record('outbox', {'id': str(uuid4()), 'aggregate_type': 'graph_revision',
                'aggregate_id': revision['id'], 'event_type': 'graph.prepare', 'status': 'pending',
                'payload': {'material_id': revision['material_id'], 'space_id': space_id, 'correction_id': correction_id,
                            'snapshot_hash': revision['snapshot_hash']}, 'created_at': now()})
            return {'run_id': correction['run_id'], 'status': 'queued', 'candidate_revision_id': revision['id']}
        return self.commands._execute('correction.confirm', space_id, {'correction_id': correction_id, **payload}, key, change)
