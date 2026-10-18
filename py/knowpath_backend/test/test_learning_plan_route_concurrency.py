"""A database wait in a plan route must not stall unrelated HTTP requests."""
import asyncio
import threading

import httpx
import pytest

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.config import LearningSettings


@pytest.mark.parametrize('method,path,operation,payload', [
    ('GET', '/api/v1/learning-spaces/space/workbench', 'get_workbench', None),
    ('GET', '/api/v1/plans/plan', 'get_plan', None),
    ('GET', '/api/v1/plans/plan/tasks/task/learning-record', 'get_learning_record', None),
    ('PATCH', '/api/v1/plans/plan/tasks/task', 'update_task', {'expected_plan_version': 1, 'status': 'completed'}),
])
def test_plan_database_wait_does_not_block_http_loop(monkeypatch, method, path, operation, payload):
    app = create_app(settings=LearningSettings(local_token='test-token'))
    entered, release = threading.Event(), threading.Event()
    def waiting(*args, **kwargs):
        entered.set()
        assert release.wait(3), 'database wait was not released'
        return {}
    monkeypatch.setattr(app.state.learning_state, operation, waiting)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
            headers={'X-Local-Token': 'test-token'}) as client:
            blocked = asyncio.create_task(client.request(method, path, json=payload))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                health = await asyncio.wait_for(client.get('/api/v1/health', headers={'X-Local-Token': ''}), .5)
                assert health.status_code == 200
                assert not release.is_set(), 'database wait must still be active when health returns'
            finally:
                release.set()
                response = await blocked
            assert response.status_code == 200
    # Release from another OS thread even when a broken async route blocks the loop.
    watchdog = threading.Timer(1.5, release.set)
    watchdog.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        watchdog.cancel()
