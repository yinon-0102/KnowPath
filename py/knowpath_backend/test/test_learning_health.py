"""Detailed health reflects dependency probes; public health never probes."""
from fastapi.testclient import TestClient
from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.config import LearningSettings


def test_health_reports_dependency_failure_without_details(monkeypatch):
    from knowpath_backend.learning import health
    monkeypatch.setattr(health, "probe_sql", lambda repository: "unavailable")
    monkeypatch.setattr(health, "probe_neo4j", lambda: "not_configured")
    monkeypatch.setattr(health, "probe_qdrant", lambda: "ok")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with TestClient(create_app(settings=LearningSettings(local_token="test-token"))) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}
        details = client.get("/api/v1/health", headers={"X-Local-Token": "test-token"}).json()
        assert details["status"] == "degraded"
        assert details["dependencies"]["mysql"] == "unavailable"
        assert details["dependencies"]["qdrant"] == "ok"
        assert details["dependencies"]["llm"]["status"] == "not_configured"


def test_sql_probe_bounds_stalled_mysql_handshake():
    import socket
    import threading
    import time
    from types import SimpleNamespace
    from sqlalchemy import create_engine
    from knowpath_backend.learning.health import probe_sql
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()
    def stall():
        connection, _ = listener.accept()
        with connection:
            stop.wait(8)
    thread = threading.Thread(target=stall, daemon=True)
    thread.start()
    engine = create_engine(f"mysql+pymysql://probe:unused@127.0.0.1:{listener.getsockname()[1]}/probe")
    started = time.monotonic()
    try:
        assert probe_sql(SimpleNamespace(unit_of_work=SimpleNamespace(engine=engine))) == "unavailable"
        assert time.monotonic() - started < 5
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)
        engine.dispose()
