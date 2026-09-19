"""Consume durable assessment/message jobs without resetting unrelated Runs."""
import argparse
import json
import sys
import time

from dotenv import load_dotenv

from knowpath_backend.learning.persistence.db import create_db_engine
from .model_tasks import ModelTaskWorker
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from .state import LearningState


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
    engine = state = None
    try:
        engine = create_db_engine()
        state = LearningState(SqlAlchemyMaterialRepository(engine))
        worker = ModelTaskWorker(state.assessment_service, state.message_service,
                                 lease_seconds=args.lease_seconds, max_attempts=args.max_attempts,
                                 max_execution_seconds=args.max_execution_seconds)
        while True:
            try:
                handled = worker.run_once()
            except Exception:
                # Provider/connection errors may contain credentials; keep stdout
                # machine-readable and diagnostics independent of exception text.
                print("MODEL_WORKER_STORAGE_UNAVAILABLE", file=sys.stderr)
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
        print("MODEL_WORKER_START_FAILED", file=sys.stderr)
        return 1
    finally:
        try:
            if state is not None:
                close = getattr(state.message_service.retriever, "close", None)
                if close is not None:
                    close()
        finally:
            if engine is not None:
                engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
