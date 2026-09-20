"""Explicit preparation of a parsed material version's rebuildable vector index.

This command does not publish a graph or switch a learning space's bindings.
The current topic projection uses graph_version=1; GraphService/outbox integration
will supply published graph snapshots when implemented.
"""
import argparse
import json
import sys

from dotenv import load_dotenv

from knowpath_backend.learning.persistence.db import create_db_engine
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.spaces.service import topics_for_version
from knowpath_backend.learning.rag.retrieval import RetrievalError, configured_vector_retriever


def index_material_version(repository, retriever, version_id):
    # Detached immutable snapshot: external requests never hold SQL locks.
    version = repository.get_version(version_id)
    if version is None or repository.get_material(version.material_id) is None:
        raise RetrievalError("MATERIAL_VERSION_UNAVAILABLE")
    if version.status != "ready" or not version.chunks:
        raise RetrievalError("MATERIAL_VERSION_NOT_READY")
    chunks = {chunk.id: chunk for chunk in version.chunks}
    sources = []
    for topic in topics_for_version(version.material_id, version):
        for ref in topic["source_refs"]:
            sources.append({**ref, "topic_id": topic["id"], "topic_name": topic["name"],
                            "graph_version": 1, "text": chunks[ref["chunk_id"]].text})
    result = retriever.index(sources)
    return {**result, "material_version_id": version_id, "graph_version": 1}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare a parsed material version's Qdrant index; does not publish graphs")
    parser.add_argument("--version-id", required=True, help="Exact material_version_id to index")
    args = parser.parse_args(argv)
    load_dotenv()
    engine = retriever = None
    try:
        engine = create_db_engine()
        retriever = configured_vector_retriever()
        result = index_material_version(SqlAlchemyMaterialRepository(engine), retriever, args.version_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except RetrievalError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    except Exception:
        print("VECTOR_INDEX_BUILD_FAILED", file=sys.stderr)
        return 1
    finally:
        if retriever is not None:
            retriever.close()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
