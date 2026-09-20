import json
import time
import gzip

import httpx
import pytest

from knowpath_backend.learning.rag.runtime import DeadlineTransport


def journal():
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    return RequestJournal()


def test_completed_call_usage_survives_a_later_transport_failure(monkeypatch):
    log = journal()
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 2:
            raise httpx.ReadTimeout('SECRET provider body')
        return httpx.Response(200, json={'usage': {'prompt_tokens': 80, 'completion_tokens': 9},
                                        'choices': [{'message': {'content': 'SECRET source'}}]})

    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(handle))
    with httpx.Client(transport=DeadlineTransport(time.monotonic() + 5, journal=log)) as client:
        log.set_stage('generation')
        client.post('https://provider.invalid/chat/completions', json={'messages': ['SECRET']})
        log.set_stage('verification')
        with pytest.raises(httpx.ReadTimeout):
            client.post('https://provider.invalid/chat/completions', json={})
    snapshot = log.seal('failed')
    assert snapshot['physical_calls'] == 2
    assert snapshot['calls'][0]['usage'] == {'prompt_tokens': 80, 'completion_tokens': 9}
    assert snapshot['calls'][1]['failure_kind'] == 'read_timeout'
    assert snapshot['calls'][1]['billing_status'] == 'unknown'
    assert snapshot['stage'] == 'verification'
    assert 'SECRET' not in json.dumps(snapshot)
    assert 'provider.invalid' not in json.dumps(snapshot)


def test_deadline_seals_pending_calls_and_ignores_late_completion():
    log = journal()
    log.set_stage('verification')
    identifier = log.begin_call('verification')
    snapshot = log.seal('deadline')
    assert snapshot['calls'][0]['status'] == 'unknown'
    log.finish_call(identifier, status='succeeded', usage={'prompt_tokens': 7})
    assert log.snapshot() == snapshot


def test_compressed_response_usage_is_retained_at_transport_boundary(monkeypatch):
    log = journal()
    data = gzip.compress(b'{"usage":{"prompt_tokens":18,"completion_tokens":4}}')
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: httpx.Response(200, headers={'Content-Encoding':'gzip'}, stream=httpx.ByteStream(data))))
    with httpx.Client(transport=DeadlineTransport(time.monotonic()+5, journal=log)) as client:
        assert client.get('https://provider.invalid/chat/completions').json()['usage']['prompt_tokens'] == 18
    assert log.snapshot()['calls'][0]['usage'] == {'prompt_tokens':18, 'completion_tokens':4}


@pytest.mark.parametrize('code,kind', [(401, 'authentication'), (429, 'rate_limited'), (503, 'server_error')])
def test_http_failure_classification_is_safe(monkeypatch, code, kind):
    log = journal()
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: httpx.Response(code, json={'message': 'SECRET'})))
    with httpx.Client(transport=DeadlineTransport(time.monotonic() + 5, journal=log)) as client:
        client.post('https://provider.invalid/chat/completions', json={})
    row = log.snapshot()['calls'][0]
    assert row['failure_kind'] == kind
    assert row['http_status'] == code
    assert row['billing_status'] == 'unknown'
    assert 'SECRET' not in json.dumps(row)


def test_invalid_usage_and_untrusted_journal_cannot_inject_diagnostics():
    from knowpath_backend.learning.rag.diagnostics import safe_journal_snapshot
    log = journal()
    identifier = log.begin_call('generation')
    log.finish_call(identifier, status='succeeded', usage={'prompt_tokens': True, 'api_key': 'SECRET'})
    assert log.snapshot()['calls'][0]['usage'] == {}
    assert safe_journal_snapshot({'stage': 'SECRET', 'calls': [{'api_key': 'SECRET'}]}) == {}


def test_malformed_response_retains_known_usage_and_previous_calls():
    # The journal represents physical transport usage even when model content
    # fails subsequent structured validation.
    log = journal()
    identifier = log.begin_call('generation')
    log.finish_call(identifier, status='succeeded', usage={'prompt_tokens': 23, 'completion_tokens': 12})
    log.fail_phase('response_schema')
    value = log.seal('failed')
    assert value['calls'][0]['usage']['completion_tokens'] == 12
    assert value['failure_kind'] == 'response_schema'


def test_watchdog_retains_active_stage_and_previous_call(build_workspace, monkeypatch):
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.rag.runtime import ConfiguredPipeline
    from knowpath_backend.learning.rag.verification import VerificationError
    state, *_ = build_workspace
    instance = ConfiguredPipeline(state.material_repository, state.space_service, LearningSettings(), 'a')
    monkeypatch.setenv('RAG_DEADLINE_SECONDS', '.05')

    def slow(question, *, deadline, journal, **kwargs):
        first = journal.begin_call('generation')
        journal.finish_call(first, status='succeeded', usage={'prompt_tokens': 23})
        journal.begin_call('verification')
        time.sleep(.2)
        return {'text': 'must not publish'}

    monkeypatch.setattr(instance, '_answer', slow)
    with pytest.raises(VerificationError) as caught:
        instance.answer('question')
    assert caught.value.code == 'RAG_DEADLINE_EXCEEDED'
    saved = caught.value.call_journal
    assert saved['stage'] == 'verification'
    assert saved['calls'][0]['usage']['prompt_tokens'] == 23
    assert saved['calls'][1]['status'] == 'unknown'
    assert saved['status'] == 'deadline'


from knowpath_backend.test.test_rag_building import build_workspace


def test_stream_total_stage_deadline_preserves_time_for_verification(monkeypatch):
    from threading import Event
    from knowpath_backend.learning.rag.verification import VerificationError
    closed = Event()
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(20):
                if closed.wait(.03):
                    return
                yield b' '
        def close(self):
            closed.set()
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: httpx.Response(200, stream=SlowStream())))
    start = time.monotonic()
    with httpx.Client(transport=DeadlineTransport(start + 5)) as client:
        with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
            client.post('https://provider.invalid/chat/completions', json={},
                        extensions={'rag_stage_deadline': start + .12})
    assert time.monotonic() - start < .5
    # Closing the real stream belongs to its I/O owner and must not delay the
    # deadline caller. A cooperative provider closes on its next return.
    assert closed.wait(.5)


def test_preheader_deadline_returns_before_late_response_and_blocked_close(monkeypatch):
    from threading import Event
    from knowpath_backend.learning.rag.verification import VerificationError
    release_headers, release_close, close_started, close_done = (Event() for _ in range(4))
    log = journal()
    log.set_stage('generation')

    class LateStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"usage":{"prompt_tokens":99}}'

        def close(self):
            close_started.set()
            release_close.wait(1)
            close_done.set()

    def handle(request):
        release_headers.wait(.6)
        return httpx.Response(200, stream=LateStream())

    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(handle))
    client = httpx.Client(transport=DeadlineTransport(time.monotonic() + 5, journal=log))
    started = time.monotonic()
    try:
        with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
            client.post('https://provider.invalid/chat/completions', json={},
                        extensions={'rag_stage_deadline': started + .06})
        client.close()
        assert time.monotonic() - started < .3
        frozen = log.seal('deadline')
        assert frozen['physical_calls'] == 1
        release_headers.set()
        assert close_started.wait(.5)
        assert log.snapshot() == frozen
        release_close.set()
        assert close_done.wait(.5)
        assert log.snapshot() == frozen
    finally:
        release_headers.set()
        release_close.set()
        client.close()


def test_body_deadline_does_not_synchronously_wait_for_stream_close(monkeypatch):
    from threading import Event
    from knowpath_backend.learning.rag.verification import VerificationError
    release_body, release_close, closed = Event(), Event(), Event()

    class BlockedStream(httpx.SyncByteStream):
        def __iter__(self):
            release_body.wait(.6)
            yield b'{}'

        def close(self):
            release_close.wait(.6)
            closed.set()

    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: httpx.Response(200, stream=BlockedStream())))
    client = httpx.Client(transport=DeadlineTransport(time.monotonic() + 5))
    started = time.monotonic()
    try:
        with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
            client.get('https://provider.invalid/body', extensions={'rag_stage_deadline': started + .06})
        client.close()
        assert time.monotonic() - started < .3
    finally:
        release_body.set()
        release_close.set()
        client.close()
        assert closed.wait(1)


@pytest.mark.parametrize('blocked_phase', ['headers', 'close'])
def test_stuck_http_owner_keeps_bounded_worker_slot_until_cleanup(monkeypatch, blocked_phase):
    from threading import Event, BoundedSemaphore
    from knowpath_backend.learning.rag import runtime
    from knowpath_backend.learning.rag.verification import VerificationError
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(runtime, '_HTTP_WORKER_SLOTS', slots)
    release, close_started, close_done = Event(), Event(), Event()
    sends = []

    class BlockedTransport(httpx.BaseTransport):
        def handle_request(self, request):
            sends.append(request)
            if blocked_phase == 'headers':
                release.wait(1)
            return httpx.Response(200, content=b'{}')

        def close(self):
            close_started.set()
            if blocked_phase == 'close':
                release.wait(1)
            close_done.set()

    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: BlockedTransport())
    first = httpx.Client(transport=runtime.DeadlineTransport(time.monotonic() + 5))
    second_log = journal()
    second = httpx.Client(transport=runtime.DeadlineTransport(time.monotonic() + 5, journal=second_log))
    try:
        if blocked_phase == 'headers':
            with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
                first.get('https://provider.invalid/first', extensions={'rag_stage_deadline':time.monotonic()+.06})
        else:
            assert first.get('https://provider.invalid/first').status_code == 200
        first.close()
        if blocked_phase == 'close':
            assert close_started.wait(.5)
        start = time.monotonic()
        with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
            second.get('https://provider.invalid/second', extensions={'rag_stage_deadline':start+.06})
        assert time.monotonic()-start < .3
        assert len(sends) == 1
        assert second_log.snapshot()['physical_calls'] == 0
        assert not slots.acquire(blocking=False)
    finally:
        release.set()
        first.close()
        second.close()
        assert close_done.wait(1)
        assert slots.acquire(timeout=1)
        slots.release()


def test_slow_transport_initialization_cannot_send_after_stage_deadline(monkeypatch):
    from threading import Event
    from knowpath_backend.learning.rag.verification import VerificationError
    release, closed = Event(), Event()
    sends = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            sends.append(request)
            return httpx.Response(200, content=b'{}')
        def close(self):
            closed.set()

    def create(**kwargs):
        release.wait(.6)
        return Transport()

    monkeypatch.setattr(httpx, 'HTTPTransport', create)
    log = journal()
    client = httpx.Client(transport=DeadlineTransport(time.monotonic()+5, journal=log))
    try:
        with pytest.raises(VerificationError, match='RAG_DEADLINE_EXCEEDED'):
            client.get('https://provider.invalid/init', extensions={'rag_stage_deadline':time.monotonic()+.06})
    finally:
        client.close()
        release.set()
        assert closed.wait(1)
    assert sends == []
    assert log.snapshot()['physical_calls'] == 0
