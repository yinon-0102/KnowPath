"""Create one explicitly named example through the real authenticated API."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))
from knowpath_backend.core.env import load_project_env

SOURCE = ROOT / 'docs/examples/KnowPath-Python入门体验资料.md'
MANIFEST = ROOT / '.runtime.local/usage-example-python.json'
KEY = 'knowpath-usage-example-python-20261009-v1'


def save(value):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['upload', 'prepare', 'complete', 'verify'])
    args = parser.parse_args()
    load_project_env()
    token = os.getenv('LEARNING_LOCAL_TOKEN') or (ROOT / 'py/.learning-token.local').read_text(encoding='utf-8').strip()
    data = json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.exists() else {}
    with httpx.Client(base_url='http://127.0.0.1:8000', headers={'X-Local-Token': token}, timeout=60) as client:
        def request(method, path, *, key=None, **kwargs):
            response = client.request(method, path, headers={'Idempotency-Key': key} if key else None, **kwargs)
            if response.is_error:
                body = response.json().get('error', {})
                raise RuntimeError(f"{method} {path}: {response.status_code} {body.get('code')} {body.get('message')}")
            return response.json()
        if args.stage == 'upload':
            upload = request('POST', '/api/v1/materials', key=KEY + '-upload',
                files={'file': (SOURCE.name, SOURCE.read_bytes(), 'text/markdown')}, data={'auto_ingest': 'true'})
            data.update(material_id=upload['material']['id'], version_id=upload['version']['id'], parse_run_id=upload['run_id'])
            save(data)
            print(json.dumps({'material_id': data['material_id'], 'parse_run_id': data['parse_run_id']}, ensure_ascii=False))
            return
        if args.stage == 'prepare':
            # Bounded execution of only this upload's genuine parser/index jobs.
            # Existing persistent workers may already own the jobs.
            from knowpath_backend.learning.persistence.db import create_db_engine
            from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
            from knowpath_backend.learning.state import LearningState
            from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
            from knowpath_backend.learning.workers.graph import GraphWorker
            from knowpath_backend.learning.knowledge.preparation import configured_graph_preparer
            engine = create_db_engine()
            materials = SqlAlchemyMaterialRepository.from_env(engine)
            state = LearningState(materials)
            preparer = None
            try:
                assert materials.get_material(data['material_id']), 'API/database mismatch'
                for event in state.graph_service.repository.records('outbox', event_type='material.parse', aggregate_id=data['version_id']):
                    if event['status'] in {'pending', 'processing'}:
                        MaterialParseWorker(state.graph_service).run_once(event['id'])
                for event in state.graph_service.repository.records('outbox', event_type='graph.prepare'):
                    if event['payload'].get('material_id') == data['material_id'] and event['status'] in {'pending', 'processing'}:
                        if preparer is None:
                            preparer = configured_graph_preparer(materials)
                        GraphWorker(state.graph_service, preparer).run_once(event['id'])
            finally:
                if preparer is not None:
                    preparer.close()
                close = getattr(state.message_service.retriever, 'close', None)
                if close:
                    close()
                materials.close()
                engine.dispose()
            print(json.dumps(request('GET', f"/api/v1/runs/{data['parse_run_id']}"), ensure_ascii=False))
            return
        if args.stage == 'complete':
            diff = request('GET', f"/api/v1/materials/{data['material_id']}/graph-diff")
            if diff['status'] != 'published':
                if diff['status'] != 'pending_review' or diff['conflicts']:
                    raise RuntimeError('Example graph is not ready for conflict-free publication')
                request('POST', f"/api/v1/materials/{data['material_id']}/graph-revisions/{diff['candidate_revision_id']}/publish",
                    key=KEY + '-publish', json={'expected_graph_version': diff['base_graph_version'], 'resolutions': []})
            created = request('POST', '/api/v1/learning-spaces', key=KEY + '-space', json={
                'name': '示例｜Python 入门体验', 'material_ids': [data['material_id']],
                'goal': '体验资料→计划节点→学习会话→关联测评→自动笔记流程；学习基础语法，不预设已掌握。', 'weekly_minutes': 150})
            data['space_id'] = created['id']
            save(data)
            if not data.get('scope_saved'):
                current = request('GET', f"/api/v1/learning-spaces/{data['space_id']}")
                graph = request('GET', f"/api/v1/materials/{data['material_id']}/graph")
                if not data.get('scope_payload'):
                    data['scope_payload'] = {'topic_ids': [n['id'] for n in graph['nodes'] if n.get('status') == 'active'],
                        'excluded_topic_ids': [], 'include_prerequisites': True, 'expected_version': current['space_version']}
                    save(data)
                request('POST', f"/api/v1/learning-spaces/{data['space_id']}/scope", key=KEY + '-scope', json=data['scope_payload'])
                data['scope_saved'] = True
                save(data)
            if not data.get('plan_id'):
                plan = request('POST', f"/api/v1/learning-spaces/{data['space_id']}/plans", key=KEY + '-plan',
                    json={'session_count': 5, 'minutes_per_session': 30, 'include_review': True})
                data['plan_id'] = plan['plan_id']
                save(data)
            notebook = request('GET', f"/api/v1/learning-spaces/{data['space_id']}/notebook")
            data['notebook_id'] = notebook['id']
            if notebook['chapters']:
                data['chapter_id'] = notebook['chapters'][0]['id']
                c = request('GET', f"/api/v1/note-chapters/{data['chapter_id']}")
                if not any(b.get('markdown', '').startswith('## 体验说明') for b in c['blocks']):
                    request('PATCH', f"/api/v1/note-chapters/{c['id']}", json={'expected_version': c['version'], 'blocks': [
                        {'kind': 'personal', 'markdown': '## 体验说明\n\n这是为了了解系统流程而创建的个人备注，不是测评总结，也不代表已掌握。\n\n1. 在学习计划点击第一个节点，开始学习会话。\n2. 阅读资料并按需向学习助手提问，然后结束会话。\n3. 从节点或结束会话入口发起关联测评，独立作答后提交。\n4. 返回本章，等待自动笔记追加；可以编辑正文、纠偏，查看版本历史。\n\n个人补充：在这里记录你自己的问题和想法。'}]})
            save(data)
        if args.stage in {'complete', 'verify'}:
            space = request('GET', f"/api/v1/learning-spaces/{data['space_id']}")
            plan = request('GET', f"/api/v1/plans/{data['plan_id']}")
            notebook = request('GET', f"/api/v1/learning-spaces/{data['space_id']}/notebook")
            data.update(space_name=space['name'], task_count=len(plan['tasks']), chapter_count=notebook['chapter_count'],
                completed_count=notebook['completed_count'], plan_status=plan['status'],
                space_url=f"http://127.0.0.1:5173/#/space/{data['space_id']}/overview",
                plan_url=f"http://127.0.0.1:5173/#/space/{data['space_id']}/plan",
                notes_url=f"http://127.0.0.1:5173/#/space/{data['space_id']}/notes")
            save(data)
            print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
