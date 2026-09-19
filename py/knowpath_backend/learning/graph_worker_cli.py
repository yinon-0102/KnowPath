"""Run the durable SQL graph worker separately from the API process."""
import argparse
import json
import sys
import time

from dotenv import load_dotenv

from .db import create_db_engine
from .graph_preparation import configured_graph_preparer
from .graph_worker import GraphWorker
from .material_ingest_worker import MaterialParseWorker
from .material_deletion import MaterialDeletionService, MaterialDeletionWorker, ExternalMaterialCleaner
from .repositories import SqlAlchemyMaterialRepository
from .state import LearningState


def main(argv=None):
    parser = argparse.ArgumentParser(description="Consume durable material parsing and graph preparation jobs")
    parser.add_argument("--once", action="store_true", help="Process at most one eligible job and exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.1 and 60")
    load_dotenv()
    engine = preparer = None
    try:
        engine = create_db_engine()
        state = LearningState(SqlAlchemyMaterialRepository(engine))
        preparer = configured_graph_preparer(state.material_repository)
        parse_worker = MaterialParseWorker(state.graph_service)
        worker = GraphWorker(state.graph_service, preparer)
        deletion = MaterialDeletionWorker(MaterialDeletionService(state.graph_service.repository,
            state.space_service, state.graph_service, state.run_service), ExternalMaterialCleaner(preparer))
        while True:
            try:
                handled = deletion.run_once() or worker.run_once() or parse_worker.run_once()
            except Exception:
                print("GRAPH_WORKER_STORAGE_UNAVAILABLE", file=sys.stderr)
                if args.once:
                    return 1
                handled = False
            if args.once:
                print(json.dumps({"job_claimed": handled}))
                return 0
            if not handled:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("GRAPH_WORKER_START_FAILED", file=sys.stderr)
        return 1
    finally:
        if preparer is not None:
            preparer.close()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
