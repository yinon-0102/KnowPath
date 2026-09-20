"""Opt-in live acceptance on disposable Docker stores, never the configured DB.

Run from py/: python scripts/accept_learning_backend.py --live
Only model configuration/keys are read from .env. Container endpoints are created
here and override every persistence setting. All container data is disposable.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

MODEL_SETTINGS = {"DASHSCOPE_API_KEY", "LEARNING_CHAT_MODEL", "LEARNING_CHAT_PROVIDER",
    "LEARNING_CHAT_BASE_URL", "LEARNING_CHAT_API_KEY_ENV", "LEARNING_CHAT_TIMEOUT_SECONDS",
    "LEARNING_CONTEXT_BUDGET_TOKENS", "LEARNING_EMBEDDING_MODEL", "LEARNING_EMBEDDING_PROVIDER",
    "LEARNING_EMBEDDING_BASE_URL", "LEARNING_EMBEDDING_API_KEY_ENV", "LEARNING_EMBEDDING_DIMENSION",
    "RAG_RESPONSE_FORMAT", "RAG_MODEL_INPUT_TOKENS", "RAG_MODEL_OUTPUT_TOKENS", "RAG_MAX_DRAFT_BYTES",
    "RAG_REVISION_GENERATION_SECONDS", "RAG_REVISION_VERIFICATION_SECONDS", "RAG_DEADLINE_SECONDS",
    "RAG_RERANK_BASE_URL", "RAG_RERANK_API_KEY_ENV", "RAG_RERANK_MODEL",
    "RAG_MAX_REQUEST_COST", "RAG_PRICING_FILE"}
MODEL_SETTINGS.update({"RAG_TOKEN_PROFILE_MANIFEST", "RAG_TOKEN_PROFILE_SHA256"})
MODEL_KEY_SETTINGS = ("LEARNING_CHAT_API_KEY_ENV", "LEARNING_EMBEDDING_API_KEY_ENV", "RAG_RERANK_API_KEY_ENV")


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError("DOCKER_COMMAND_FAILED:" + args[0])
    return result.stdout.strip()


class Acceptance:
    def __init__(self):
        self.containers = []
        self.report = {"started_at": datetime.now(timezone.utc).isoformat(), "checks": []}

    def checked(self, name, **details):
        self.report["checks"].append({"name": name, **details})
        print(json.dumps({"passed": name, **details}), flush=True)

    def container(self, name, image, port, env):
        args = ["run", "-d", "--name", name, "--label", "knowpath.acceptance=disposable",
                "-p", f"127.0.0.1::{port}"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        # Track before launch: Docker can create a container but fail to start it.
        self.containers.append(name)
        identifier = docker(*args, image)
        endpoint = docker("port", identifier, f"{port}/tcp").splitlines()[0]
        assert endpoint.startswith("127.0.0.1:")
        return endpoint.split(":")[-1]

    def configure_models(self, env_file=None):
        from dotenv import dotenv_values
        values = dotenv_values(env_file or BACKEND / ".env")
        allowed = set(MODEL_SETTINGS)
        for setting in MODEL_KEY_SETTINGS:
            key_name = os.getenv(setting) or values.get(setting)
            if key_name:
                allowed.add(key_name)
        for key in allowed:
            if values.get(key) and not os.getenv(key):
                os.environ[key] = values[key]
        os.environ.pop("LEARNING_CONFIG_FILE", None)
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        from knowpath_backend.learning.config import LearningSettings
        settings = LearningSettings.from_env()
        if not all(os.getenv(key) for key in (settings.chat_api_key_env, settings.embedding_api_key_env)):
            raise RuntimeError("MODEL_KEYS_NOT_CONFIGURED")
        if settings.chat_provider != "dashscope" or settings.embedding_provider != "dashscope":
            raise RuntimeError("DASHSCOPE_REQUIRED_FOR_THIS_ACCEPTANCE")
        self.report["models"] = {"chat": settings.chat_model, "embedding": settings.embedding_model}
        return settings

    def configure_storage(self):
        """Provision disposable stores without reading or changing business settings."""
        prefix = "knowpath-accept-" + uuid4().hex[:10]
        mysql = self.container(prefix + "-mysql", "mysql:8.4", 3306,
                               {"MYSQL_ROOT_PASSWORD": "acceptance-root", "MYSQL_DATABASE": "knowpath_acceptance",
                                "MYSQL_USER": "acceptance", "MYSQL_PASSWORD": "acceptance-only"})
        neo4j = self.container(prefix + "-neo4j", "neo4j:5.26-community", 7687,
                               {"NEO4J_AUTH": "neo4j/acceptance-only", "NEO4J_server_memory_heap_max__size": "512M",
                                "NEO4J_server_memory_pagecache_size": "256M"})
        qdrant = self.container(prefix + "-qdrant", "qdrant/qdrant:v1.13.6", 6333, {})
        environment = dict(DATABASE_URL=f"mysql+pymysql://acceptance:acceptance-only@127.0.0.1:{mysql}/knowpath_acceptance",
                          LEARNING_PERSISTENCE="sql", LEARNING_RETRIEVAL_BACKEND="qdrant",
                          NEO4J_URI=f"bolt://127.0.0.1:{neo4j}", NEO4J_USERNAME="neo4j", NEO4J_PASSWORD="acceptance-only",
                          QDRANT_URL=f"http://127.0.0.1:{qdrant}", QDRANT_COLLECTION="knowpath_acceptance",
                          QDRANT_API_KEY="", LEARNING_LOCAL_TOKEN="acceptance-local-token-for-isolated-run")
        environment.update(LEARNING_TEST_MYSQL_URL=environment["DATABASE_URL"],
                           LEARNING_TEST_MYSQL_ADMIN_URL=f"mysql+pymysql://root:acceptance-root@127.0.0.1:{mysql}/mysql",
                           LEARNING_TEST_NEO4J_URI=environment["NEO4J_URI"],
                           LEARNING_TEST_QDRANT_URL=environment["QDRANT_URL"],
                           RAG_TEST_QDRANT_URL=environment["QDRANT_URL"],
                           NEO4J_DATABASE="neo4j", PYTHON_DOTENV_DISABLED="1")
        self.report["services"] = {"mysql": "mysql:8.4", "neo4j": "neo4j:5.26-community", "qdrant": "qdrant/qdrant:v1.13.6"}
        self.checked("isolated_containers_created", count=3)
        return environment

    def configure(self):
        settings = self.configure_models()
        os.environ.update(self.configure_storage())
        return settings

    def run(self):
        from sqlalchemy import create_engine, inspect, text
        from alembic import command
        from alembic.config import Config
        from fastapi.testclient import TestClient
        from knowpath_backend.learning.api import create_app
        from knowpath_backend.learning.config import LearningSettings
        from knowpath_backend.learning.materials.service import MaterialService
        from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
        from knowpath_backend.learning.knowledge.preparation import configured_graph_preparer
        from knowpath_backend.learning.workers.graph import GraphWorker
        from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
        from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
        from knowpath_backend.learning.startup import recover_legacy_runs
        from knowpath_backend.learning.health import report

        self.configure()
        settings = LearningSettings.from_env()
        engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True,
                               connect_args={"connect_timeout": 3, "read_timeout": 30, "write_timeout": 30})
        with ExitStack() as stack:
            stack.callback(engine.dispose)
            repo = SqlAlchemyMaterialRepository(engine)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                health = report(repo, settings)
                if all(health["dependencies"][key] == "ok" for key in ("mysql", "neo4j", "qdrant")):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("ISOLATED_STORAGE_START_TIMEOUT")
            config = Config(str(BACKEND / "alembic.ini"))
            config.set_main_option("script_location", str(BACKEND / "migrations"))
            command.upgrade(config, "head")
            with engine.connect() as connection:
                head = connection.scalar(text("SELECT version_num FROM alembic_version"))
            self.checked("migrations_and_storage", head=head, table_count=len(inspect(engine).get_table_names()))

            def open_client():
                app = create_app(MaterialService(SqlAlchemyMaterialRepository(engine)), settings)
                client = stack.enter_context(TestClient(app))
                client.headers["X-Local-Token"] = settings.local_token
                return client, app.state.learning_state
            client, state = open_client()
            def request(method, path, **kwargs):
                kwargs.setdefault("headers", {})["Idempotency-Key"] = "accept-" + uuid4().hex
                response = getattr(client, method)("/api/v1" + path, **kwargs)
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP_{response.status_code}:" + path)
                return response.json()
            def completed(run_id):
                run = state.get_run(run_id)
                if run["status"] != "succeeded":
                    code = (run.get("error") or {}).get("code", run["status"])
                    raise RuntimeError("RUN_NOT_SUCCEEDED:" + str(code))
                return run
            def model_event(response):
                completed(response["run_id"])
                return response

            material = request("post", "/materials", files={"file": ("synthetic-functions.md",
                b"# Functions\n\nA function groups reusable behavior under a name. Parameters receive input values. "
                b"Arguments are values supplied at a call. A return statement provides the result. "
                b"A pure function returns the same output for the same inputs without changing outside state. "
                b"Local variables are visible within their function. Testing a function compares actual and expected results. "
                b"For example, add(a, b) returns a + b; add(2, 3) returns 5. A default parameter is used when an argument is omitted. "
                b"Recursion calls the same function on a smaller problem and needs a base case to stop. "
                b"Calling factorial(0) returns 1; factorial(n) for positive n returns n times factorial(n-1).",
                "text/markdown")})
            material_id = material["material"]["id"]
            preparer = configured_graph_preparer(state.material_repository)
            stack.callback(preparer.close)
            for kind, worker in (("material.parse", MaterialParseWorker(state.graph_service, max_attempts=1)),
                                 ("graph.prepare", GraphWorker(state.graph_service, preparer, max_attempts=1))):
                events = state.graph_service.repository.records("outbox", event_type=kind)
                event = next(e for e in events if e["payload"]["run_id"] == material["run_id"])
                assert worker.run_once(event["id"])
            completed(material["run_id"])
            diff = request("get", f"/materials/{material_id}/graph-diff")
            assert diff["status"] == "pending_review" and not diff["conflicts"]
            request("post", f'/materials/{material_id}/graph-revisions/{diff["candidate_revision_id"]}/publish',
                    json={"expected_graph_version": 0, "resolutions": []})
            topics = request("get", f"/materials/{material_id}/topics")["items"]
            assert topics
            with preparer.graph.driver.session() as session:
                nodes = session.run("MATCH (n:KPTopicVariant) RETURN count(n) AS count").single()["count"]
            assert nodes > 0
            self.checked("upload_graph_publication_real_embedding", topics=len(topics), neo4j_topic_nodes=nodes)

            space = request("post", "/learning-spaces", json={"name": "Disposable acceptance", "material_ids": [material_id]})
            space_id = space["id"]
            request("post", f"/learning-spaces/{space_id}/scope", json={"topic_ids": [topics[0]["id"]], "expected_version": 1})
            def assessment(kind):
                result = model_event(request("post", f"/learning-spaces/{space_id}/assessments", json={"kind": kind,
                    "question_count": 5, "question_types": ["single_choice"], "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}}))
                identifier = result["assessment_id"]
                questions = request("get", f"/assessments/{identifier}")["questions"]
                assert len(questions) == 5 and all("answer_key" not in q for q in questions)
                # First option is an explicit synthetic learner answer, not an oracle.
                request("post", f"/assessments/{identifier}/attempts", json={"answers": [
                    {"question_id": q["id"], "answer": q["options"][0]["id"], "expected_answer_revision": 0} for q in questions]})
                finalized = request("post", f"/assessments/{identifier}/finalize", json={})
                completed(finalized["run_id"])
                result = request("get", f"/assessments/{identifier}/result")
                self.checked(kind + "_generated_and_graded", questions=len(questions))
                return result
            assessment("diagnostic")
            plan = request("post", f"/learning-spaces/{space_id}/plans", json={"session_count": 3, "minutes_per_session": 30})
            plan_body = request("get", f'/plans/{plan["plan_id"]}')
            task = next(t for t in plan_body["tasks"] if t["status"] == "pending")
            session = request("post", f'/plans/{plan["plan_id"]}/sessions', json={"task_id": task["id"]})
            request("post", f'/sessions/{session["id"]}/events', json={"type": "request_explanation", "topic_id": topics[0]["id"]})
            first = model_event(request("post", f"/learning-spaces/{space_id}/messages", json={
                "message": "I struggle with recursion base cases. Explain why factorial(0) is needed using the source.", "session_id": session["id"]}))
            run = completed(first["run_id"])
            answer = next(e["data"] for e in run["events"] if e["event"] == "message.completed")
            assert answer["citations"]
            stream = client.get(f'/api/v1/runs/{first["run_id"]}/events')
            assert stream.status_code == 200 and "event: message.completed" in stream.text
            ids = [int(line[4:]) for line in stream.text.splitlines() if line.startswith("id: ")]
            resumed = client.get(f'/api/v1/runs/{first["run_id"]}/events', headers={"Last-Event-ID": str(ids[-2])})
            resumed_ids = [int(line[4:]) for line in resumed.text.splitlines() if line.startswith("id: ")]
            assert resumed_ids == [ids[-1]]
            request("post", f'/sessions/{session["id"]}/finish', json={})
            self.checked("plan_session_grounded_message_sse_resume", event_count=len(ids))

            # Queue without executing; construct fresh services, reconcile startup,
            # then consume the durable task. No provider mock participates.
            queued = state.send_message(space_id, {"message": "Recall my recursion base case difficulty and explain it."},
                                        idempotency_key="accept-restart-message", durable=True)
            client, state = open_client()
            recover_legacy_runs(state.run_service, state.message_service.repository)
            event = next(e for e in state.message_service.repository.records("outbox", event_type="message.generate")
                         if e["payload"]["run_id"] == queued["run_id"])
            assert ModelTaskWorker(state.assessment_service, state.message_service, max_attempts=1).run_once(event["id"])
            completed(queued["run_id"])
            row = state.message_service.repository.get_record("messages", event["aggregate_id"])
            assert row["snapshot"]["memory"]["recall"]
            assert row["snapshot"]["context_report"]["estimated_tokens"] <= settings.context_budget_tokens
            self.checked("durable_worker_restart_and_scoped_memory")
            retest = assessment("retest")
            client, state = open_client()
            evidence = request("get", f"/learning-spaces/{space_id}/evidence")["items"]
            retest_evidence = [row for row in evidence if row["assessment_id"] == retest["assessment_id"]]
            assert len(retest_evidence) == 5
            self.checked("retest_evidence_persisted", count=len(retest_evidence))
            self.report["status"] = "passed"

    def cleanup(self):
        failures = []
        for identifier in reversed(self.containers):
            try:
                if not docker("ps", "-aq", "--filter", f"name=^/{identifier}$"):
                    continue
                label = docker("inspect", "--format", '{{index .Config.Labels "knowpath.acceptance"}}', identifier)
                if label != "disposable":
                    raise RuntimeError("OWNER_LABEL_MISMATCH")
                docker("rm", "-f", "-v", identifier)
            except Exception:
                failures.append(identifier)
        self.report["cleanup"] = {"removed_or_absent": len(self.containers) - len(failures), "remaining_containers": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use real model calls and disposable containers")
    parser.add_argument("--report", type=Path, default=Path(os.environ["TEMP"]) / "knowpath-live-acceptance.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this acceptance makes a bounded number of paid model calls")
    acceptance = Acceptance()
    try:
        acceptance.run()
    except Exception as exc:
        # Never print raw upstream errors, credentials or connection URLs.
        safe = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith(("DOCKER_", "MODEL_KEYS_", "DASHSCOPE_", "ISOLATED_", "HTTP_", "RUN_NOT_")) else type(exc).__name__
        acceptance.report.update(status="failed", failure=safe)
        frames = traceback.extract_tb(exc.__traceback__)
        acceptance.report["failure_location"] = [{"file": Path(frame.filename).name, "line": frame.lineno}
                                                 for frame in frames if BACKEND in Path(frame.filename).parents]
        print(json.dumps({"failure": safe}), flush=True)
    finally:
        acceptance.cleanup()
        args.report.write_text(json.dumps(acceptance.report, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(args.report), "status": acceptance.report.get("status"), "cleanup": acceptance.report["cleanup"]}), flush=True)
    return 0 if acceptance.report.get("status") == "passed" and not acceptance.report["cleanup"]["remaining_containers"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
