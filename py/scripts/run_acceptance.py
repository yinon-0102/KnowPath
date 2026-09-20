"""Run offline, disposable-store, or explicitly opted-in real-model validation.

From py/: python scripts/run_acceptance.py --profile offline|store
Paid smoke: ... --profile model --live --model-env /explicit/model.env
Each invocation owns its containers, random loopback ports and report directory.
The model smoke covers embedding, reranking, generation and verification; the
separately frozen development evaluation must also be run before model acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from scripts.accept_learning_backend import Acceptance, MODEL_SETTINGS, MODEL_KEY_SETTINGS


def profile_environment():
    """Never inherit configured business stores or implicit paid-model opt-ins."""
    key_names = {value for key, value in os.environ.items() if key.endswith("API_KEY_ENV")}
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("LEARNING_", "RAG_", "NEO4J_", "QDRANT_"))
           and key not in key_names and not key.endswith("API_KEY")
           and key not in {"DATABASE_URL", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}}
    env.update(PYTHON_DOTENV_DISABLED="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(BACKEND), DATABASE_URL="sqlite+pysqlite:///:memory:",
               LEARNING_PERSISTENCE="memory", LEARNING_RETRIEVAL_BACKEND="keyword")
    return env


def storage_test_environment(acceptance):
    environment = acceptance.configure_storage()
    # Test factories explicitly inject their repositories/providers and exercise
    # auth separately. Legacy live-acceptance defaults would change those tests.
    environment.update(LEARNING_PERSISTENCE="memory", LEARNING_RETRIEVAL_BACKEND="keyword")
    environment.pop("LEARNING_LOCAL_TOKEN", None)
    return environment


def model_environment(acceptance, model_file):
    environment = profile_environment()
    allowed = set(MODEL_SETTINGS)
    allowed.update(os.environ[key] for key in MODEL_KEY_SETTINGS if os.environ.get(key))
    environment.update({key: os.environ[key] for key in allowed if os.environ.get(key)})
    original = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(environment)
        acceptance.configure_models(model_file)
        return dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_arguments():
    files = {str(path.relative_to(BACKEND)).replace("\\", "/") for pattern in
             ("test_learning*.py", "test_rag*.py", "test_acceptance*.py")
             for path in (BACKEND / "knowpath_backend/test").glob(pattern)}
    matrix = json.loads(Path(__file__).with_name("acceptance_matrix.json").read_text(encoding="utf-8-sig"))
    required = set().union(*(set(matrix[key]) for key in ("offline", "store", "model", "not_applicable")))
    return sorted(files | {node for node in required if node.split("::")[0] not in files})


def prepare_storage(acceptance, env):
    """Probe only newly provisioned endpoints, then migrate only their database."""
    from sqlalchemy import create_engine, inspect, text
    from neo4j import GraphDatabase
    from qdrant_client import QdrantClient

    engine = create_engine(env["DATABASE_URL"], hide_parameters=True,
                           connect_args={"connect_timeout": 2, "read_timeout": 3, "write_timeout": 3})
    graph = GraphDatabase.driver(env["NEO4J_URI"], auth=(env["NEO4J_USERNAME"], env["NEO4J_PASSWORD"]),
                                 connection_timeout=2, connection_acquisition_timeout=2)
    vector = QdrantClient(url=env["QDRANT_URL"], timeout=2, check_compatibility=False)
    try:
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            try:
                with engine.connect() as connection:
                    mysql_version = connection.scalar(text("SELECT VERSION()"))
                graph.verify_connectivity()
                vector.get_collections()
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("ISOLATED_STORAGE_START_TIMEOUT")
        result = subprocess.run([sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"),
                                 "upgrade", "head"], cwd=BACKEND, env=env,
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError("ISOLATED_MIGRATION_FAILED")
        with engine.connect() as connection:
            head = connection.scalar(text("SELECT version_num FROM alembic_version"))
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        config = Config(str(BACKEND / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND / "migrations"))
        if head != ScriptDirectory.from_config(config).get_current_head():
            raise RuntimeError("ISOLATED_SCHEMA_NOT_HEAD")
        with graph.session() as session:
            neo4j_version = session.run("CALL dbms.components() YIELD versions RETURN versions[0] AS version").single()["version"]
        acceptance.report["service_versions"] = {"mysql": mysql_version, "neo4j": neo4j_version,
                                                 "qdrant": vector.info().version}
        acceptance.checked("migrations_and_storage", head=head, table_count=len(inspect(engine).get_table_names()))
    finally:
        vector.close()
        graph.close()
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("offline", "store", "model"))
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--model-env", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if args.profile == "model" and (not args.live or not args.model_env or not args.model_env.is_file()):
        parser.error("model requires --live and an existing explicit --model-env file")
    report_dir = (args.report_dir or BACKEND / ".rag-evaluation" / (
        args.profile + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])).resolve()
    # Reusing a report directory would let stale output masquerade as this run.
    report_dir.mkdir(parents=True, exist_ok=False)
    acceptance = Acceptance()
    acceptance.report["profile"] = args.profile
    acceptance.report["gate_report"] = str(report_dir / "gate.json")
    env = profile_environment()
    try:
        if args.profile == "model":
            env = model_environment(acceptance, args.model_env.resolve())
            env["RAG_TEST_REAL_MODELS"] = "1"
        if args.profile in {"store", "model"}:
            env.update(storage_test_environment(acceptance))
            prepare_storage(acceptance, env)
        command = [sys.executable, "-m", "scripts.acceptance_gate", "--profile", args.profile,
                   "--report", str(report_dir / "gate.json"), "--", "-q", "--tb=short",
                   "-o", "junit_family=xunit1",
                   "--basetemp=" + str(report_dir / "tmp"),
                   "--junitxml=" + str(report_dir / "junit.xml"), *test_arguments()]
        # Capture pytest diagnostics in the run artifact; do not echo potentially
        # sensitive provider/connection exceptions to the invoking terminal.
        with (report_dir / "pytest.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT)
        gate_path = report_dir / "gate.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else {}
        acceptance.report.update(status="passed" if result.returncode == 0 and gate.get("status") == "passed" else "failed",
                                 expected=len(gate.get("expected", [])), executed=len(gate.get("executed", [])),
                                 gate_errors=gate.get("gate_errors", ["missing_gate_report"]))
    except Exception as exc:
        safe = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith(("ISOLATED_", "DOCKER_", "MODEL_KEYS_", "DASHSCOPE_")) else type(exc).__name__
        acceptance.report.update(status="failed", failure=safe)
    finally:
        acceptance.cleanup()
        if acceptance.report["cleanup"]["remaining_containers"]:
            acceptance.report["status"] = "failed"
        (report_dir / "report.json").write_text(json.dumps(acceptance.report, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(report_dir / "report.json"), "status": acceptance.report.get("status"),
                          "expected": acceptance.report.get("expected", 0), "executed": acceptance.report.get("executed", 0),
                          "cleanup": acceptance.report["cleanup"]}), flush=True)
    return 0 if acceptance.report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
