"""Frozen HTTP and MySQL contracts captured before the directory refactor."""

import json
import copy
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.api.contract import ContractRoute
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.persistence.db import Base
from knowpath_backend.learning.rag.retrieval import KeywordRetriever


FIXTURES = Path(__file__).parent / "fixtures"


def mysql_schema_contract():
    """Compile offline: preserve columns, constraints, defaults and indexes."""
    dialect = mysql.dialect()
    return {
        table.name: {
            "table": str(CreateTable(table).compile(dialect=dialect)),
            "indexes": sorted(
                str(CreateIndex(index).compile(dialect=dialect))
                for index in table.indexes
            ),
        }
        for table in sorted(Base.metadata.tables.values(), key=lambda table: table.name)
    }


def isolated_app(*, token=None):
    return create_app(
        settings=LearningSettings(local_token=token),
        source_retriever=KeywordRetriever(),
    )


def test_full_openapi_matches_pre_refactor_contract():
    expected = json.loads((FIXTURES / "learning_openapi.json").read_text(encoding="utf-8"))
    app = isolated_app()

    actual = copy.deepcopy(app.openapi())
    for action, schema in [('retry', 'IngestMaterial'), ('reparse', 'IngestMaterial'), ('rebuild', 'ReconcileGraph')]:
        route = actual['paths'].pop('/api/v1/materials/{material_id}/'+action)
        assert set(route) == {'post'}
        operation = route['post']
        assert operation['requestBody']['content']['application/json']['schema'] == {'$ref': '#/components/schemas/'+schema}
        assert '202' in operation['responses']
        assert {p['name'] for p in operation['parameters']} == {'material_id', 'Idempotency-Key'}
    for path in ('/api/v1/materials/{material_id}/topics', '/api/v1/materials/{material_id}/graph'):
        parameters = actual['paths'][path]['get']['parameters']
        additions = {p['name']: p for p in parameters if p['name'] in {'graph_revision_id', 'graph_version', 'legacy_snapshot'}}
        assert additions == {
            'graph_revision_id': {'name': 'graph_revision_id', 'in': 'query', 'required': False,
                'schema': {'anyOf': [{'type': 'string', 'minLength': 1}, {'type': 'null'}], 'title': 'Graph Revision Id'}},
            'graph_version': {'name': 'graph_version', 'in': 'query', 'required': False,
                'schema': {'anyOf': [{'type': 'integer', 'minimum': 1}, {'type': 'null'}], 'title': 'Graph Version'}},
            'legacy_snapshot': {'name': 'legacy_snapshot', 'in': 'query', 'required': False,
                'schema': {'type': 'boolean', 'title': 'Legacy Snapshot', 'default': False}},
        }
        parameters[:] = [p for p in parameters if p['name'] not in additions]
    recovery = actual['paths']['/api/v1/learning-spaces/{space_id}/assessments'].pop('get')
    assert recovery['operationId'] == 'recoverable_assessments_api_v1_learning_spaces__space_id__assessments_get'
    assert recovery['parameters'] == [
        {'name': 'space_id', 'in': 'path', 'required': True, 'schema': {'type': 'string', 'title': 'Space Id'}},
        {'name': 'plan_id', 'in': 'query', 'required': False,
         'schema': {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'title': 'Plan Id'}},
        {'name': 'task_id', 'in': 'query', 'required': False,
         'schema': {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'title': 'Task Id'}},
    ]
    # T01 completion provenance and T04 opt-in feedback are approved additive
    # contracts. Assert their full shape before comparing the historical base.
    schemas = actual['components']['schemas']
    assert schemas['RecordAttempt']['properties'].pop('include_feedback') == {
        'type': 'boolean', 'title': 'Include Feedback', 'default': False}
    assert schemas['RecordAttempt']['required'] == ['answers']
    assert schemas['UpdateTask']['properties'].pop('source') == {
        'type': 'string', 'enum': ['manual', 'assessment'], 'title': 'Source', 'default': 'manual'}
    assert schemas['UpdateTask']['properties'].pop('assessment_id') == {
        'anyOf': [{'type': 'string', 'minLength': 1}, {'type': 'null'}], 'title': 'Assessment Id'}
    assert schemas['UpdateTask']['required'] == ['expected_plan_version', 'status']
    parameters = actual['paths']['/api/v1/plans/{plan_id}/tasks/{task_id}']['patch']['parameters']
    assert parameters.pop() == {'name': 'Idempotency-Key', 'in': 'header', 'required': False,
        'schema': {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'title': 'Idempotency-Key'}}
    message_scope = json.loads((FIXTURES / 'learning_message_scope_openapi.json').read_text(encoding='utf-8'))
    assert actual['components']['schemas']['SendMessage']['properties'].pop('material_ids') == message_scope['material_ids_property']
    # Preserve historical contracts; freeze each additive review operation and
    # its request schema separately from the original pre-refactor snapshot.
    reviews = json.loads((FIXTURES / 'learning_question_reviews_openapi.json').read_text(encoding='utf-8'))
    algorithms = json.loads((FIXTURES / 'learning_algorithms_openapi.json').read_text(encoding='utf-8'))
    notes = json.loads((FIXTURES / 'learning_notes_openapi.json').read_text(encoding='utf-8'))
    # Reviewed additive contracts from lesson/reading, ordering and the approved
    # personalization plan. Keep the pre-refactor snapshot unchanged.
    personalization = json.loads((FIXTURES / 'learning_personalization_openapi.json').read_text(encoding='utf-8'))
    assert set(personalization['paths']) == {
        '/api/v1/assessments/{assessment_id}/activity',
        '/api/v1/learning-spaces/{space_id}/conversations/{session_id}/messages',
        '/api/v1/learning-spaces/{space_id}/nodes/{node_id}/lessons',
        '/api/v1/learning-spaces/{space_id}/policy-recommendations',
        '/api/v1/plan-adjustments/{identifier}/revert',
        '/api/v1/plans/{plan_id}/adjustments', '/api/v1/plans/{plan_id}/order',
        '/api/v1/plans/{plan_id}/order-preview', '/api/v1/plans/{plan_id}/refresh',
        '/api/v1/plans/{plan_id}/schedule', '/api/v1/policy-recommendations/{identifier}/apply',
        '/api/v1/sessions/{session_id}/activity', '/api/v1/study-lessons/{lesson_id}',
        '/api/v1/study-lessons/{lesson_id}/retry',
    }
    assert set(personalization['schemas']) == {
        'Activity', 'AnswerActivity', 'ApplyRecommendation', 'CreateLesson', 'OrderPlan',
        'ReadingContext', 'Refresh', 'Request', 'Revert', 'Schedule',
    }
    assert {name: set(props) for name, props in personalization['added_properties'].items()} == {
        'CreatePlan': {'calendar_timezone', 'daily_minutes', 'horizon_days', 'material_order', 'order_mode', 'schedule_mode'},
        'Preferences': {'automatic_adjustment', 'lesson_adaptation', 'lesson_instructions', 'lesson_style', 'time_personalization'},
        'SendMessage': {'answer_mode', 'lesson_id', 'reading_context'},
    }
    workbench_paths = {'/api/v1/learning-spaces/{space_id}/workbench',
                       '/api/v1/learning-spaces/{space_id}/progress',
                       '/api/v1/plans/{plan_id}/tasks/{task_id}/learning-record'}
    assert set(actual['paths']) - set(expected['paths']) == {
        '/api/v1/learning-spaces/{space_id}/citations/resolve',
        '/api/v1/materials/{material_id}/graph',
        '/api/v1/materials/{material_id}/versions/{version_id}/file',
    } | set(reviews['paths']) | set(algorithms['paths']) | set(notes['paths']) | set(personalization['paths']) | workbench_paths
    assert {path: actual['paths'].pop(path) for path in personalization['paths']} == personalization['paths']
    for path in workbench_paths:
        assert set(actual['paths'].pop(path)) == {'get'}
    assert {path: actual['paths'].pop(path) for path in notes['paths']} == notes['paths']
    assert {path: actual['paths'].pop(path) for path in algorithms['paths']} == algorithms['paths']
    assert {path: actual['paths'].pop(path) for path in reviews['paths']} == reviews['paths']
    graph_projection = actual['paths'].pop('/api/v1/materials/{material_id}/graph')
    assert set(graph_projection) == {'get'}
    assert {param['name'] for param in graph_projection['get']['parameters']} == {'material_id', 'version_id', 'include_inactive', 'include_sources'}
    material_file = actual['paths'].pop('/api/v1/materials/{material_id}/versions/{version_id}/file')
    assert set(material_file) == {'get'}
    assert {param['name'] for param in material_file['get']['parameters']} == {'material_id', 'version_id', 'space_id'}
    actual['paths'].pop('/api/v1/learning-spaces/{space_id}/citations/resolve')
    assert set(actual['components']['schemas']) - set(expected['components']['schemas']) == {'Citation','SourceSpan'} | set(reviews['schemas']) | set(algorithms['schemas']) | set(notes['schemas']) | set(personalization['schemas'])
    assert {name: actual['components']['schemas'].pop(name) for name in personalization['schemas']} == personalization['schemas']
    for name, properties in personalization['added_properties'].items():
        assert {field: actual['components']['schemas'][name]['properties'].pop(field) for field in properties} == properties
    assert {name: actual['components']['schemas'].pop(name) for name in notes['schemas']} == notes['schemas']
    assert {name: actual['components']['schemas'].pop(name) for name in algorithms['schemas']} == algorithms['schemas']
    assert actual['components']['schemas']['CreateAssessment']['properties'].pop('adaptive') == algorithms['adaptive_property']
    for field, title in [('plan_id', 'Plan Id'), ('task_id', 'Task Id'), ('learning_session_id', 'Learning Session Id')]:
        assert actual['components']['schemas']['CreateAssessment']['properties'].pop(field) == {
            'anyOf': [{'type': 'string', 'minLength': 1}, {'type': 'null'}], 'title': title}
    assert {name: actual['components']['schemas'].pop(name) for name in reviews['schemas']} == reviews['schemas']
    for name in ('Citation','SourceSpan'):
        actual['components']['schemas'].pop(name)
    assert actual == expected
    def api_routes(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif (included := getattr(route, "original_router", None)) is not None:
                # FastAPI 0.141+ retains included routers instead of flattening.
                yield from api_routes(included.routes)

    routes = list(api_routes(app.routes))
    added_operations = sum(len(operations) for operations in personalization['paths'].values())
    assert len(routes) == 73 + added_operations
    assert all(isinstance(route, ContractRoute) for route in routes)
    assert len({(route.path, method) for route in routes for method in route.methods}) == 73 + added_operations


def test_mysql_schema_matches_pre_refactor_contract():
    expected = json.loads((FIXTURES / "learning_mysql_schema.json").read_text(encoding="utf-8"))

    actual = mysql_schema_contract()
    addition = "\tparser_version VARCHAR(128) NOT NULL DEFAULT 'legacy_unknown', \n"
    assert addition in actual['material_versions']['table']
    actual['material_versions']['table'] = actual['material_versions']['table'].replace(addition, '')
    addition = "\tparse_manifest JSON, \n"
    assert addition in actual['material_versions']['table']
    actual['material_versions']['table'] = actual['material_versions']['table'].replace(addition, '')
    # 仅替换本次显式迁移的表快照，其他旧表仍逐字匹配历史契约。
    expected['material_raw_files'] = json.loads((FIXTURES / 'learning_raw_object_schema.json').read_text(encoding='utf-8'))
    from knowpath_backend.learning.persistence.rag_models import TABLES
    notes = json.loads((FIXTURES / 'learning_notes_mysql_schema.json').read_text(encoding='utf-8'))
    assert {name: actual[name] for name in notes} == notes
    personalization = json.loads((FIXTURES / 'learning_personalization_mysql_schema.json').read_text(encoding='utf-8'))
    assert set(personalization) == {'study_lessons', 'budget_comparisons', 'plan_adjustments', 'policy_recommendations'}
    assert {name: actual[name] for name in personalization} == personalization
    additions = set(TABLES) | set(notes) | set(personalization)
    assert set(actual) - set(expected) == additions
    assert {name:value for name,value in actual.items() if name not in additions} == expected


def test_multiple_apps_keep_auth_materials_and_runs_isolated():
    first_app = isolated_app(token="first-app-token")
    second_app = isolated_app(token="second-app-token")
    first_headers = {"X-Local-Token": "first-app-token"}
    second_headers = {"X-Local-Token": "second-app-token"}

    with TestClient(first_app) as first, TestClient(second_app) as second:
        assert first.get("/api/v1/materials", headers=second_headers).status_code == 401
        assert second.get("/api/v1/materials", headers=first_headers).status_code == 401
        created_ids = []
        for client, headers, content in (
            (first, first_headers, b"First application"),
            (second, second_headers, b"Second application"),
        ):
            response = client.post(
                "/api/v1/materials",
                files={"file": ("notes.txt", content, "text/plain")},
                data={"auto_ingest": "false"},
                headers={**headers, "Idempotency-Key": "shared-key"},
            )
            assert response.status_code == 201
            created_ids.append(response.json()["material"]["id"])
        assert created_ids[0] != created_ids[1]
        for client, headers, own_id, other_id in (
            (first, first_headers, created_ids[0], created_ids[1]),
            (second, second_headers, created_ids[1], created_ids[0]),
        ):
            response = client.get("/api/v1/materials", headers=headers)
            assert response.status_code == 200
            assert [item["id"] for item in response.json()["items"]] == [own_id]
            assert client.get(f"/api/v1/materials/{other_id}", headers=headers).status_code == 404

        first_run = first_app.state.learning_state.run("first", status="running")
        second_run = second_app.state.learning_state.run("second", status="running")
        assert first.get(f"/api/v1/runs/{second_run['id']}", headers=first_headers).status_code == 404
        assert second.get(f"/api/v1/runs/{first_run['id']}", headers=second_headers).status_code == 404
        cancelled = first.post(
            f"/api/v1/runs/{first_run['id']}/cancel", json={}, headers=first_headers,
        )
        assert cancelled.status_code == 202
        assert second.get(f"/api/v1/runs/{second_run['id']}", headers=second_headers).json()["status"] == "running"
