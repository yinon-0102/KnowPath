"""Run the durable SQL graph worker separately from the API process."""
import argparse
import json
import logging
import time

from knowpath_backend.observability import configure_logging, shutdown_logging, log_event


from knowpath_backend.core.env import load_project_env

# Backward-compatible hook for tests and external launchers that patch load_dotenv.
load_dotenv = load_project_env

from knowpath_backend.learning.persistence.db import create_db_engine
from knowpath_backend.learning.knowledge.preparation import configured_graph_preparer
from knowpath_backend.learning.workers.graph import GraphWorker
from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
from knowpath_backend.learning.materials.deletion import MaterialDeletionService, MaterialDeletionWorker, ExternalMaterialCleaner
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState

logger = logging.getLogger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Consume durable material parsing and graph preparation jobs")
    parser.add_argument("--once", action="store_true", help="Process at most one eligible job and exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.1 and 60")
    load_dotenv()
    configure_logging('graph-worker')
    failures = 0
    ready = False
    engine = preparer = state = materials = None
    try:
        engine = create_db_engine()
        materials = SqlAlchemyMaterialRepository.from_env(engine)
        state = LearningState(materials)
        preparer = configured_graph_preparer(state.material_repository)
        parse_worker = MaterialParseWorker(state.graph_service)
        worker = GraphWorker(state.graph_service, preparer)
        deletion = MaterialDeletionWorker(MaterialDeletionService(state.graph_service.repository,
            state.space_service, state.graph_service, state.run_service), ExternalMaterialCleaner(preparer))
        ready = True
        log_event(logger, 'worker.started', poll_seconds=args.poll_seconds)
        while True:
            try:
                handled = deletion.run_once() or worker.run_once() or parse_worker.run_once()
                if failures:
                    log_event(logger, 'worker.poll.recovered', consecutive_failures=failures)
                failures = 0
            except Exception as exc:
                failures += 1
                if failures == 1 or failures % 15 == 0:
                    log_event(logger, 'worker.poll.failed', level=logging.ERROR, exc=exc, consecutive_failures=failures)
                if args.once:
                    return 1
                handled = False
            if args.once:
                print(json.dumps({"job_claimed": handled}))
                return 0
            if not handled:
                log_event(logger, 'worker.idle', level=logging.DEBUG)
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log_event(logger, 'worker.runtime.failed' if ready else 'worker.start.failed', level=logging.ERROR, exc=exc)
        return 1
    finally:
        try:
            try:
                if preparer is not None:
                    preparer.close()
            finally:
                try:
                    if materials is not None:
                        materials.close()
                finally:
                    if engine is not None:
                        engine.dispose()
        except Exception as exc:
            log_event(logger, 'worker.shutdown.failed', level=logging.ERROR, exc=exc)
        finally:
            log_event(logger, 'worker.stopped')
            shutdown_logging()

if __name__ == "__main__":
    raise SystemExit(main())