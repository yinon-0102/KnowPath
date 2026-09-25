"""python -m knowpath_backend.rag_eval.cli freeze|run|report."""
import argparse
import json
import os
from pathlib import Path
import sys

from .dataset import freeze, mode_spec, verify_freeze
from .runner import run
from .scoring import report


def runtime_configuration(settings):
    """Exact supported runtime settings; unsupported custom budgets fail closed."""
    from knowpath_backend.learning.rag.contracts import RetrievalBudget
    from knowpath_backend.learning.rag.reranking import DEFAULT_RERANK_ENDPOINT, DEFAULT_RERANK_MODEL
    from knowpath_backend.learning.rag.spending import spending_configuration
    from knowpath_backend.learning.rag.runtime import model_budget_configuration
    from knowpath_backend.learning.rag.protocol_config import protocol_configuration
    from knowpath_backend.learning.persistence.db import database_identity
    models = {name: getattr(settings, name) for name in ("chat_provider", "chat_model", "chat_base_url",
        "embedding_provider", "embedding_model", "embedding_dimension", "embedding_base_url")}
    models.update(rerank_model=os.getenv("RAG_RERANK_MODEL") or DEFAULT_RERANK_MODEL,
                  rerank_endpoint=os.getenv("RAG_RERANK_BASE_URL") or DEFAULT_RERANK_ENDPOINT)
    budgets = dict(RetrievalBudget().model_dump(), deadline_seconds=float(os.getenv("RAG_DEADLINE_SECONDS", "120")),
        chat_timeout_seconds=settings.chat_timeout_seconds, model_context_tokens=settings.context_budget_tokens,
        **model_budget_configuration(settings), rerank_input_tokens=90000,
        rerank_candidates=40, transport_retries=0, spending=spending_configuration())
    return dict(database_identity=database_identity(), models=models, budgets=budgets, prompts={"implementation": "frozen_python_sources",
                'protocol':protocol_configuration(settings)},
                environment={"qdrant_url": os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
                             "collection_prefix": os.getenv("RAG_COLLECTION_PREFIX", "knowpath_rag_content")})


def configured_runtime(frozen):
    config = frozen["config"]
    if any(mode_spec(mode, config)["execution"] == "injected_offline"
           for mode in config.get("modes", ("a", "b1"))):
        raise ValueError("parent_merge and typed_edge require injected offline evaluation")
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.persistence.db import create_db_engine
    from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
    from knowpath_backend.learning.persistence.rag_repository import SqlRagRepository
    from knowpath_backend.learning.rag.lifecycle import ManifestLifecycle
    from knowpath_backend.learning.rag.runtime import ConfiguredPipeline
    from knowpath_backend.learning.state import LearningState
    settings = LearningSettings.from_env()
    actual = runtime_configuration(settings)
    non_database_keys = tuple(key for key in actual if key != "database_identity")
    if any(config.get(key) != actual[key] for key in non_database_keys):
        raise ValueError("frozen models/prompts/budgets/environment do not match the configured runtime")
    if not isinstance(config.get("database_identity"), dict):
        raise ValueError("legacy freeze missing database identity")
    if config["database_identity"] != actual["database_identity"]:
        raise ValueError("frozen models/prompts/budgets/environment do not match the configured runtime")
    configurations = {}
    for binding in config["runtime_bindings"].values():
        for manifest, value in binding["manifest_configuration_hashes"].items():
            if manifest in configurations and configurations[manifest] != value:
                raise ValueError("inconsistent frozen manifest profile")
            configurations[manifest] = value
    if config["profile"] != {"manifest_configuration_hashes": configurations}:
        raise ValueError("frozen profile must enumerate exact manifest configuration hashes")
    engine = create_db_engine()
    try:
        state = LearningState(SqlAlchemyMaterialRepository(engine))
        lifecycle = ManifestLifecycle(SqlRagRepository(engine))
        def factory(mode):
            if runtime_configuration(LearningSettings.from_env()) != actual:
                raise ValueError("runtime configuration changed after freeze")
            return ConfiguredPipeline(state.material_repository, state.space_service, settings, mode)
        factory.real_runtime = True
        def resolver(space_id):
            scope = state.space_service.rag_scope_snapshot(space_id)
            return {**scope.model_dump(mode="json"), "manifests": lifecycle.pin(scope, require_b1=True)}
        return factory, resolver, engine
    except Exception:
        engine.dispose()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Frozen exploratory paired RAG evaluation")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("freeze")
    create.add_argument("--config", required=True)
    create.add_argument("--output", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--freeze", required=True)
    execute.add_argument("--output", required=True)
    execute.add_argument("--partition", choices=("dev", "heldout"), default="dev")
    score = commands.add_parser("report")
    score.add_argument("--freeze", required=True)
    score.add_argument("--results", required=True)
    score.add_argument("--reviews")
    score.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze(args.config, args.output)
            print(json.dumps({"freeze_id": result["freeze_id"], "file_count": len(result["file_hashes"])}))
        elif args.command == "run":
            from .dataset import authorize_partition
            frozen, rows, split = verify_freeze(args.freeze)
            authorize_partition(frozen, rows, split, args.partition)
            factory, resolver, engine = configured_runtime(frozen)
            try:
                records = run(args.freeze, args.output, partition=args.partition,
                              pipeline_factory=factory, scope_resolver=resolver)
            finally:
                engine.dispose()
            print(json.dumps({"attempted": len(records), "service_successes": sum(r["service_success"] for r in records)}))
        else:
            result = report(args.freeze, args.results, args.reviews)
            with Path(args.output).open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            print(json.dumps({"decision": result["decision"], "missing_execution_rows": result["missing_execution_rows"]}))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        # CLI may touch configured providers/DBs. Do not echo endpoints, DSNs,
        # connection exceptions or accidental secrets in malformed config.
        print("EVALUATION_CONFIGURATION_OR_INPUT_INVALID", file=sys.stderr)
        return 2
    except Exception:
        print("EVALUATION_RUNTIME_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
