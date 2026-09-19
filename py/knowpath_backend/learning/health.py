"""Bounded dependency probes; never return connection strings or provider errors."""
import os
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import text, create_engine
from sqlalchemy.pool import NullPool


def probe_sql(repository):
    uow = getattr(repository, "unit_of_work", None)
    if uow is None:
        return "not_configured"
    probe_engine = None
    try:
        engine = uow.engine
        if engine.dialect.name == "mysql":
            # Separate probe connections must never inherit unbounded business
            # pool waits or driver reads (including the initial handshake).
            probe_engine = create_engine(engine.url, poolclass=NullPool,
                connect_args={"connect_timeout": 2, "read_timeout": 2, "write_timeout": 2})
            engine = probe_engine
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "unavailable"
    finally:
        if probe_engine is not None:
            probe_engine.dispose()


def probe_neo4j():
    if not os.getenv("NEO4J_URI"):
        return "not_configured"
    try:
        from neo4j import GraphDatabase
        from .storage import GraphSettings
        settings = GraphSettings.from_env()
        with GraphDatabase.driver(settings.uri, auth=(settings.username, settings.password),
                                  connection_timeout=2, connection_acquisition_timeout=2) as driver:
            driver.verify_connectivity()
        return "ok"
    except Exception:
        return "unavailable"


def probe_qdrant():
    if not os.getenv("QDRANT_URL") and os.getenv("LEARNING_RETRIEVAL_BACKEND") != "qdrant":
        return "not_configured"
    client = None
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
                              api_key=os.getenv("QDRANT_API_KEY") or None, timeout=2, check_compatibility=False)
        client.get_collections()
        return "ok"
    except Exception:
        return "unavailable"
    finally:
        if client is not None:
            client.close()


def report(repository, settings):
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(probe_sql, repository), pool.submit(probe_neo4j), pool.submit(probe_qdrant)]
        sql, graph, vector = [future.result() for future in futures]
    configured = lambda name: "configured" if os.getenv(name, "").strip() else "not_configured"
    return {"status": "degraded" if "unavailable" in (sql, graph, vector) else "ok",
            "version": "0.1.0", "runtime": "keel-learning", "dependencies": {
                "mysql": sql, "neo4j": graph, "vector_store": "qdrant", "qdrant": vector,
                "llm": {"provider": settings.chat_provider, "model": settings.chat_model, "status": configured(settings.chat_api_key_env)},
                "embedding": {"provider": settings.embedding_provider, "model": settings.embedding_model,
                              "dimension": settings.embedding_dimension, "status": configured(settings.embedding_api_key_env)}}}
