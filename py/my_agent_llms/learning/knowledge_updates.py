"""Explicit adoption of published knowledge with transactional evidence invalidation."""
from __future__ import annotations

import copy
import json
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .assessments import revision
from .errors import DomainConflict
from .spaces import SpaceService, now


class KnowledgeBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    material_id: str = Field(min_length=1)
    material_version_id: str = Field(min_length=1)
    graph_version: StrictInt = Field(ge=1)


class ApplyKnowledgeUpdates(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bindings: list[KnowledgeBinding] = Field(min_length=1)
    expected_space_version: StrictInt = Field(ge=0)

    @model_validator(mode='after')
    def unique_materials(self):
        if len({b.material_id for b in self.bindings}) != len(self.bindings):
            raise ValueError('bindings 中 material_id 不可重复')
        return self


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class KnowledgeUpdateService:
    def __init__(self, repository, spaces, graphs, runs):
        self.repository, self.spaces, self.graphs, self.runs = repository, spaces, graphs, runs
        self.commands = SpaceService(repository, spaces.materials)

    @staticmethod
    def _binding(row):
        return {k: row[k] for k in ('material_id', 'material_version_id', 'graph_version')} | {'graph_revision_id': row['id']}

    def _histories(self, space):
        histories = {}
        for binding in sorted(space['bindings'], key=lambda b: b['material_id']):
            identifier = binding['material_id']
            self.graphs._material(identifier)  # Same material locks as graph publication.
            histories[identifier] = self.graphs._history(identifier)
        return histories

    def _sources(self, refs):
        hashes = []
        for ref in refs:
            version = self.spaces.materials.get_version(ref['material_version_id'])
            if version is None:
                raise DomainConflict('BOUND_VERSION_UNAVAILABLE', '绑定来源版本不可用')
            chunk = next((c for c in version.chunks if c.id == ref['chunk_id']), None)
            if chunk is None:
                raise DomainConflict('BOUND_VERSION_UNAVAILABLE', '绑定来源片段不可用')
            hashes.append(chunk.content_hash)
        return sorted(hashes)

    def _signature(self, topic):
        fields = ('name', 'description', 'kind', 'parent_id', 'level', 'status', 'automatic_questions', 'prerequisites')
        value = {k: topic.get(k) for k in fields}
        value['automatic_questions'] = topic.get('automatic_questions', True)
        value['sources'] = self._sources(topic.get('source_refs', []))
        value['alternatives'] = [self._signature(t) for t in topic.get('alternatives', [])]
        return digest(value)

    def _relations(self, bindings):
        result = {}
        for binding in bindings:
            identifier = binding.get('graph_revision_id')
            if not identifier:
                continue
            row = self.graphs.repository.get_record('graph_revisions', identifier)
            for edge in row['publication']['snapshot']['relations']:
                if edge.get('status') in {'rejected', 'superseded', 'inactive'}:
                    continue
                value = {k: edge.get(k) for k in ('from_id', 'to_id', 'type', 'status')}
                value['sources'] = self._sources(edge.get('source_refs', []))
                result[digest(value)] = value
        return result

    def _impact(self, space, bindings):
        old = {t['id']: t for t in self.spaces.bound_topics(space)}
        target = {**space, 'bindings': bindings}
        new = {t['id']: t for t in self.spaces.bound_topics(target)}
        affected = {tid for tid in old.keys() | new.keys()
                    if tid not in old or tid not in new or self._signature(old[tid]) != self._signature(new[tid])}
        before, after = self._relations(space['bindings']), self._relations(bindings)
        endpoints = set()
        for key in before.keys() ^ after.keys():
            edge = before.get(key) or after[key]
            endpoints.update((edge['from_id'], edge['to_id']))
        affected.update(endpoints)
        # Compatibility aliases share the canonical topic's evidence version.
        for topic in [*old.values(), *new.values()]:
            if topic.get('canonical_topic_id', topic['id']) in endpoints:
                affected.add(topic['id'])
        return old, new, sorted(affected)

    def _preview(self, space, bindings, updates):
        _, _, affected = self._impact(space, bindings)
        questions = sorted({q['id'] for a in self.repository.records('assessments', space_id=space['id'], lock=False)
                            for q in a['questions'] if q['topic_id'] in affected})
        plans = [p['id'] for p in self.repository.records('plans', space_id=space['id'], lock=False)
                 if p['status'] != 'superseded'] if updates else []
        return {'space_id': space['id'], 'bindings': copy.deepcopy(space['bindings']),
                'available_updates': updates, 'affected_topic_ids': affected,
                'invalidated_question_ids': questions,
                'plan_impact': {'status': 'needs_replan', 'plan_ids': plans} if plans else None}

    def preview(self, space_id):
        with self.repository.transaction():
            space = self.spaces.repository.get(space_id)
            histories = self._histories(space)
            bindings, updates = [], []
            for current in space['bindings']:
                latest = self.graphs._published(histories[current['material_id']])
                if latest and (latest['graph_version'] > current['graph_version'] or
                               (not current.get('graph_revision_id') and latest['graph_version'] == current['graph_version'])):
                    selected = self._binding(latest)
                    updates.append({**selected, 'previous_graph_version': current['graph_version']})
                else:
                    selected = copy.deepcopy(current)
                bindings.append(selected)
            return self._preview(space, bindings, updates)

    def apply(self, space_id, payload, key=None):
        payload = ApplyKnowledgeUpdates.model_validate(payload).model_dump()
        payload['bindings'].sort(key=lambda b: b['material_id'])
        def change():
            space = self.spaces.repository.get(space_id)
            if payload['expected_space_version'] != space['space_version']:
                raise DomainConflict('VERSION_CONFLICT', '学习空间版本已变化')
            current = {b['material_id']: b for b in space['bindings']}
            if set(current) != {b['material_id'] for b in payload['bindings']}:
                raise DomainConflict('BINDING_MISMATCH', '必须提供当前空间全部资料的绑定，不可增删资料')
            histories = self._histories(space)
            bindings = []
            for wanted in payload['bindings']:
                previous = current[wanted['material_id']]
                candidates = [r for r in histories[wanted['material_id']] if r['material_version_id'] == wanted['material_version_id']
                    and r.get('graph_version') == wanted['graph_version'] and r['status'] in {'published', 'superseded'} and r.get('publication')]
                if not candidates:
                    raise DomainConflict('GRAPH_NOT_PUBLISHED', '只能采用已正式发布的图谱快照')
                if wanted['graph_version'] < previous['graph_version']:
                    raise DomainConflict('VERSION_CONFLICT', '不可回退空间图谱版本')
                selected = self._binding(candidates[-1])
                if selected['graph_revision_id'] == previous.get('graph_revision_id'):
                    selected = copy.deepcopy(previous)
                bindings.append(selected)
            if all(b == current[b['material_id']] for b in bindings):
                raise DomainConflict('NO_KNOWLEDGE_UPDATES', '空间已使用所选快照')
            old, new, affected = self._impact(space, bindings)
            for binding in bindings:
                binding['topic_revision_ids'] = {}
                for tid, topic in new.items():
                    if not any(ref['material_id'] == binding['material_id'] for ref in topic['source_refs']):
                        continue
                    binding['topic_revision_ids'][tid] = (revision(old[tid]) if tid in old and tid not in affected else
                        digest([space_id, space['space_version'] + 1, tid, self._signature(topic)]))
            timestamp = now()
            space.update(bindings=bindings, space_version=space['space_version'] + 1,
                         scope_version=space['scope_version'] + 1, updated_at=timestamp)
            # Keep explicit selection (including removed IDs) so disappearance never expands scope.
            stale_count = 0
            if affected:
                space['state_version'] += 1
                for item in self.repository.records('states', space_id=space_id):
                    if item['topic_id'] in affected:
                        item.update(score_validity='stale', status='needs_review', state_version=space['state_version'])
                        self.repository.put_record('states', item)
                        stale_count += 1
            for plan in self.repository.records('plans', space_id=space_id):
                if plan['status'] != 'superseded':
                    plan.setdefault('config', {})['invalidated_topic_ids'] = sorted(set(plan.get('config', {}).get('invalidated_topic_ids', [])) | set(affected))
                    plan.update(status='needs_replan', version=plan['version'] + 1)
                    self.repository.put_record('plans', plan)
            self.spaces.repository.put(space)
            run = self.runs.create('knowledge_update_apply', status='running')
            result = {'run_id': run['id'], 'space_id': space_id, 'space_version': space['space_version'],
                      'affected_topic_ids': affected, 'stale_state_count': stale_count, 'plan_replan_run_id': None}
            self.runs.complete(run['id'], {'type': 'learning_space', 'id': space_id, **result})
            return result
        return self.commands._execute('knowledge_updates.apply', space_id, payload, key, change)
