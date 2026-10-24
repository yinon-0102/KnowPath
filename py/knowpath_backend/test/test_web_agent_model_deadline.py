"""Every physical model call shares one absolute, cancellable turn deadline."""
import time
from threading import Event, Timer

import httpx
import pytest

from knowpath_backend.core.turn import TurnCancelled
from knowpath_backend.learning.agent_runtime import model
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.verification import VerificationError
from knowpath_backend.test.web_agent_support import completion


def settings(monkeypatch):
    monkeypatch.setenv('OFFLINE_DEADLINE_KEY', 'synthetic')
    return LearningSettings(chat_api_key_env='OFFLINE_DEADLINE_KEY',
        chat_base_url='https://synthetic.invalid/v1', chat_timeout_seconds=60)


def test_physical_timeout_shrinks_and_expired_turn_refuses_dispatch(monkeypatch):
    configured = settings(monkeypatch)
    clock, requests = [100.0], []
    monkeypatch.setattr(model.time, 'monotonic', lambda: clock[0])
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion('ok').model_dump(mode='json'))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        port = model.OneShotChatPort(configured, client=client, deadline=160)
        body = dict(model=configured.chat_model, messages=[])
        port.complete(body)
        clock[0] = 155
        port.complete(body)
        assert requests[0].extensions['timeout']['read'] == 60
        assert requests[1].extensions['timeout']['read'] == 5
        clock[0] = 160
        with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
            port.complete(body)
        assert len(requests) == 2


def test_late_cancelled_response_never_reaches_callback(monkeypatch):
    configured = settings(monkeypatch)
    cancelled, chunks = [], []
    def handler(request):
        cancelled.append(True)
        return httpx.Response(200, json=completion('private').model_dump(mode='json'))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        port = model.OneShotChatPort(configured, client=client,
            deadline=time.monotonic()+10, should_cancel=lambda: bool(cancelled))
        with pytest.raises(TurnCancelled):
            port.complete(dict(model=configured.chat_model, messages=[]), on_text_chunk=chunks.append)
    assert not chunks


def test_owned_transport_absolute_deadline_unblocks_stuck_provider(monkeypatch):
    configured = settings(monkeypatch)
    released, entered = Event(), Event()
    def handler(request):
        entered.set()
        released.wait(2)
        return httpx.Response(200, json=completion('late').model_dump(mode='json'))
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(handler))
    port = model.OneShotChatPort(configured, deadline=time.monotonic()+.1)
    started = time.monotonic()
    try:
        with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
            port.complete(dict(model=configured.chat_model, messages=[]))
        assert entered.is_set()
        assert time.monotonic()-started < .5
    finally:
        released.set()


def test_owned_transport_cancel_unblocks_stuck_provider(monkeypatch):
    configured = settings(monkeypatch)
    released, cancelled, entered = Event(), Event(), Event()
    def handler(request):
        entered.set()
        released.wait(2)
        return httpx.Response(200, json=completion('late').model_dump(mode='json'))
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(handler))
    port = model.OneShotChatPort(configured, deadline=time.monotonic()+10, should_cancel=cancelled.is_set)
    timer = Timer(.1, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(TurnCancelled):
            port.complete(dict(model=configured.chat_model, messages=[]))
        assert entered.is_set()
        assert time.monotonic()-started < .6
    finally:
        released.set()
        timer.cancel()


def test_slot_saturation_can_be_cancelled_without_starting_worker(monkeypatch):
    from threading import BoundedSemaphore
    from knowpath_backend.learning.rag import runtime
    slots, cancelled = BoundedSemaphore(1), Event()
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(runtime, '_HTTP_WORKER_SLOTS', slots)
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: pytest.fail('no slot permits provider transport'))
    configured = settings(monkeypatch)
    timer = Timer(.1, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(TurnCancelled):
            model.OneShotChatPort(configured, deadline=time.monotonic()+10,
                should_cancel=cancelled.is_set).complete(dict(model=configured.chat_model, messages=[]))
        assert time.monotonic()-started < .6
        assert not slots.acquire(blocking=False)
    finally:
        timer.cancel()
        slots.release()


def test_absolute_deadline_bounds_trickling_body_and_retains_owner_slot(monkeypatch):
    from threading import BoundedSemaphore
    from knowpath_backend.learning.rag import runtime
    slots, release, exited = BoundedSemaphore(1), Event(), Event()
    monkeypatch.setattr(runtime, '_HTTP_WORKER_SLOTS', slots)
    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{'
            # The caller must stop waiting independently of socket activity.
            release.wait(2)
            yield b'}'
        def close(self):
            exited.set()
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Trickle())))
    configured = settings(monkeypatch)
    started = time.monotonic()
    try:
        with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
            model.OneShotChatPort(configured, deadline=time.monotonic()+.1).complete(
                dict(model=configured.chat_model, messages=[]))
        assert time.monotonic()-started < .5
        assert not slots.acquire(blocking=False), 'blocked I/O owner must retain its global slot'
    finally:
        release.set()
        assert exited.wait(1)
