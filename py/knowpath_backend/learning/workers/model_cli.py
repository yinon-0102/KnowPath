"""Consume durable assessment/message jobs without resetting unrelated Runs."""
import argparse
import json
import logging
import time

from knowpath_backend.observability import configure_logging, shutdown_logging, log_event


from dotenv import load_dotenv

from knowpath_backend.learning.persistence.db import create_db_engine
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState

logger = logging.getLogger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Consume durable assessment and message generation jobs")
    parser.add_argument("--once", action="store_true", help="Process at most one eligible job and exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=float, default=300.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-execution-seconds", type=float, default=1200.0,
                        help="Per-attempt renewal/publication time limit (1-3600 seconds)")
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.1 and 60")
    if not 1 <= args.lease_seconds <= 3600:
        parser.error("--lease-seconds must be between 1 and 3600")
    if not 1 <= args.max_attempts <= 20:
        parser.error("--max-attempts must be between 1 and 20")
    if not 1 <= args.max_execution_seconds <= 3600:
        parser.error("--max-execution-seconds must be between 1 and 3600")
    load_dotenv()
    configure_logging('model-worker')
    failures = 0
    ready = False
    engine = state = materials = None
    try:
        engine = create_db_engine()
        materials = SqlAlchemyMaterialRepository.from_env(engine)
        state = LearningState(materials)
        worker = ModelTaskWorker(state.assessment_service, state.message_service,
                                 lease_seconds=args.lease_seconds, max_attempts=args.max_attempts,
                                 max_execution_seconds=args.max_execution_seconds)
        ready = True
        log_event(logger, 'worker.started', poll_seconds=args.poll_seconds)
        while True:
            try:
                handled = worker.run_once()
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
                if state is not None:
                    close = getattr(state.message_service.retriever, "close", None)
                    if close is not None:
                        close()
            finally:
                try:
                    if materials is not None:
                        materials.close()
                finally:
                    if engine is not None:
                        engine.dispose()
        except Exception as exc:
            log_event(logger, 'worker.shutdown.failed', level=logging.ERROR, exc=exc)
            return 1
        finally:
            log_event(logger, 'worker.stopped')
            shutdown_logging()


if __name__ == "__main__":
    raise SystemExit(main())
