import pytest
from fastapi.testclient import TestClient
from knowpath_backend.test.test_rag_building import build_workspace
from knowpath_backend.test.test_rag_pipeline import pipeline


def test_configuration_selects_v2_and_rejects_invalid_modes(build_workspace, monkeypatch):
    from knowpath_backend.learning.rag.runtime import configured_pipeline
    state, *_ = build_workspace
    monkeypatch.setenv('LEARNING_RAG_PLUGIN', 'legacy')
    assert configured_pipeline(state.material_repository, state.space_service) is None
    monkeypatch.setenv('LEARNING_RAG_PLUGIN', 'a')
    assert configured_pipeline(state.material_repository, state.space_service).mode == 'a'
    monkeypatch.setenv('LEARNING_RAG_PLUGIN', 'unknown')
    with pytest.raises(ValueError):
        configured_pipeline(state.material_repository, state.space_service)


def test_v2_citation_route_resolves_authoritative_source_and_current_scope(build_workspace):
    from knowpath_backend.learning.api.application import create_app
    state, uploaded, space, *_ = build_workspace
    service, _ = pipeline(build_workspace)
    result = service.answer('学校应该告知谁？', space_id=space['id'])
    citation = result['citations'][0]
    app = create_app(state.material_service, rag_pipeline=service)
    with TestClient(app) as client:
        client.headers['Idempotency-Key'] = 'source-read-test'
        url = f"/api/v1/learning-spaces/{space['id']}/citations/resolve"
        response = client.post(url, json=citation)
        assert response.status_code == 200, response.text
        assert '学校' in response.json()['text']
        bad = {**citation, 'retrieval_version_id': 'wrong'}
        assert client.post(url, json=bad).status_code == 409
        state.space_service.set_scope(space['id'], {'topic_ids': ['topic_排除'], 'expected_version': 1})
        assert client.post(url, json=citation).status_code == 409


def test_runtime_rejects_late_result_without_waiting_for_provider(build_workspace, monkeypatch):
    import time
    from knowpath_backend.learning.rag.runtime import ConfiguredPipeline
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.rag.verification import VerificationError
    state, *_ = build_workspace
    instance = ConfiguredPipeline(state.material_repository, state.space_service, LearningSettings(), 'a')
    monkeypatch.setenv('RAG_DEADLINE_SECONDS', '.05')
    def slow(*args, **kwargs):
        time.sleep(.25)
        return {'text':'late'}
    monkeypatch.setattr(instance, '_answer', slow)
    started = time.monotonic()
    with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
        instance.answer('test')
    assert time.monotonic() - started < .2


def test_transport_preserves_tighter_provider_timeout_and_bounds_unset_stages(monkeypatch):
    import httpx
    import time
    from knowpath_backend.learning.rag.runtime import DeadlineTransport

    observed = []
    def handle(request):
        observed.append(dict(request.extensions['timeout']))
        return httpx.Response(200, content=b'{}')

    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(handle))
    with httpx.Client(transport=DeadlineTransport(time.monotonic() + 30)) as client:
        response = client.get('https://provider.invalid/test',
            timeout=httpx.Timeout(connect=2, read=5, write=None, pool=90))
        assert response.status_code == 200
    assert len(observed) == 1
    assert observed[0]['connect'] == 2
    assert observed[0]['read'] == 5
    assert 0 < observed[0]['write'] <= 30
    assert 0 < observed[0]['pool'] <= 30
