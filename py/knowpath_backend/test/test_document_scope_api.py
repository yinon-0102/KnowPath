"""Public HTTP schema rejects empty/out-of-space selections before dispatch."""
from fastapi.testclient import TestClient

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.retrieval import KeywordRetriever
from knowpath_backend.test.test_learning_messages import FixedAnswer


def test_http_document_selection_validation_and_frozen_response():
    generator = FixedAnswer()
    app = create_app(settings=LearningSettings(), answer_generator=generator, source_retriever=KeywordRetriever())
    state = app.state.learning_state
    materials = [state.material_service.create(filename=f'{name}.md',
        content=f'# {name}\n\nFunctions group reusable behavior.'.encode(), idempotency_key=name).material for name in ('A', 'B')]
    space = state.create_space({'name': 'Both', 'material_ids': [m.id for m in materials]})
    with TestClient(app) as client:
        client.headers['Idempotency-Key'] = 'scope-http'
        url = f"/api/v1/learning-spaces/{space['id']}/messages"
        empty = client.post(url, json={'message': 'q', 'material_ids': []})
        assert empty.status_code == 422
        assert empty.json()['error']['code'] == 'INVALID_REQUEST'
        foreign = client.post(url, json={'message': 'q', 'material_ids': ['outside-space']})
        assert foreign.status_code == 409
        assert foreign.json()['error']['code'] == 'MATERIAL_OUT_OF_SCOPE'
        assert generator.calls == []
        accepted = client.post(url, json={'message': 'Explain', 'material_ids': [materials[0].id]})
        assert accepted.status_code == 202
        assert {s['material_id'] for s in generator.calls[0]['sources']} == {materials[0].id}
        events = state.events_for(accepted.json()['run_id'])
        response = next(e['data'] for e in events if e['event'] == 'message.completed')
        assert response['request_scope']['material_ids'] == [materials[0].id]
        assert response['request_scope']['mode'] == 'selected'
