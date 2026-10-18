"""Source and update reads must not block the ASGI event loop on database I/O."""
import asyncio
import threading

import httpx
import pytest

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.config import LearningSettings


@pytest.mark.parametrize('path,target,method,result,status', [
    ('/api/v1/learning-spaces/space/knowledge-updates', 'state', 'knowledge_updates', {}, 200),
    ('/api/v1/materials', 'materials', 'list_materials', [], 200),
    ('/api/v1/materials/m/versions/v/chunks/c', 'materials', 'get_version', None, 404),
    ('/api/v1/materials/m/versions/v/file', 'materials', 'get_version', None, 404),
])
def test_source_database_wait_does_not_block_health(monkeypatch, path, target, method, result, status):
    app = create_app(settings=LearningSettings(local_token='test-token'))
    state = app.state.learning_state
    entered, release = threading.Event(), threading.Event()
    def waiting(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return result
    monkeypatch.setattr(state if target == 'state' else state.material_service.repository, method, waiting)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
            headers={'X-Local-Token': 'test-token'}) as client:
            blocked = asyncio.create_task(client.get(path))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                response = await asyncio.wait_for(client.get('/api/v1/health', headers={'X-Local-Token': ''}), .5)
                assert response.status_code == 200
                assert not release.is_set(), 'database wait stalled unrelated HTTP response'
            finally:
                release.set()
                response = await blocked
            assert response.status_code == status
    watchdog = threading.Timer(1.5, release.set)
    watchdog.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        watchdog.cancel()
