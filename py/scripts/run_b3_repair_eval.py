"""Reproduce the approved B3 repair experiment without changing the old run.

prepare: local SQLite/source copies and local Qdrant copies; no model calls.
freeze: pin repaired sources and all experiment sidecars after review.
run: 40 questions x 3 modes, once, no harness retries or silent resume.
report: score the completed or interrupted append-only log.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import zipfile

PY = Path(__file__).resolve().parents[1]
WORK = PY.parent
MAIN = WORK.parents[1] if WORK.parent.name == ".worktrees" else WORK
sys.path.insert(0, str(PY))
BASE = PY / ".rag-evaluation"
SOURCE = BASE / "tree-b3-unit-40-v1"
TARGET = BASE / "tree-b3-repair-40-v2"
PREFIX = "knowpath_rag_b3_repair_40_v2"
DATASET_HASH = "230bf4a271ba2bc2fc2d774357caf2bc2e21b46ae5eccf7c9e190b05a1d9eeb2"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def configure():
    from dotenv import load_dotenv
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.rag_eval.cli import runtime_configuration
    # Credentials are only loaded into this process and never serialized.
    load_dotenv(MAIN / "py/.env", override=True)
    old = read(SOURCE / "development.json")
    os.environ.update(
        DATABASE_URL="sqlite:///" + (TARGET / "evaluation.db").as_posix(),
        RAG_COLLECTION_PREFIX=PREFIX, QDRANT_URL="http://127.0.0.1:6333",
        RAG_MODEL_INPUT_TOKENS="14000", RAG_MODEL_OUTPUT_TOKENS="2000",
        LEARNING_CONTEXT_BUDGET_TOKENS="16000", RAG_DEADLINE_SECONDS="120",
        RAG_MAX_DRAFT_BYTES="4000", RAG_RESPONSE_FORMAT="json_object",
        RAG_REVISION_GENERATION_SECONDS="10", RAG_REVISION_VERIFICATION_SECONDS="25",
    )
    settings = LearningSettings.from_env()
    actual = runtime_configuration(settings)
    if actual["models"] != old["models"] or actual["prompts"] != old["prompts"]:
        raise ValueError("MODEL_OR_PROTOCOL_CHANGED")
    expected_budget = dict(old["budgets"], model_input_tokens=14000)
    if actual["budgets"] != expected_budget:
        raise ValueError("UNEXPECTED_BUDGET_CHANGE")
    return settings, actual


def canonical_rows():
    from knowpath_backend.learning.rag.units import build_units
    from knowpath_backend.learning.rag.unit_first import UnitFirstPlugin
    bindings = read(SOURCE / "development.json")["runtime_bindings"]
    versions = {rv for binding in bindings.values() for rv in binding["expected_retrieval_versions"]}
    with sqlite3.connect((TARGET / "evaluation.db").as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        trees = {rv: json.loads(payload)["tree_version_id"] for rv, payload in
                 conn.execute("SELECT retrieval_version_id,payload FROM rag_manifests WHERE status='ready'") if rv in versions}
        groups = {rv: [] for rv in versions}
        for rv, payload in conn.execute("SELECT retrieval_version_id,payload FROM rag_chunks"):
            if rv in versions:
                groups[rv].append(dict(json.loads(payload), tree_version_id=trees[rv]))
    leaves = [row for rv in sorted(groups) for row in groups[rv]]
    units = [unit for rv in sorted(groups) for unit in build_units(groups[rv], tree_version_id=trees[rv])]
    unit_rows = [UnitFirstPlugin._unit_row(unit) for unit in units]
    if len(leaves) != 970 or len(units) != 934:
        raise ValueError("CANONICAL_INDEX_COUNT_CHANGED")
    old_units = {unit["unit_id"]: unit for unit in read(SOURCE / "unit-manifest.json")["units"]}
    for unit in units:
        if any(old_units[unit.unit_id][key] != value for key, value in {
            "anchor_id": unit.anchor_id, "leaf_ids": list(unit.leaf_ids),
            "source_map_hash": unit.source_map_hash, "tree_version_id": unit.tree_version_id,
        }.items()):
            raise ValueError("CANONICAL_UNIT_CHANGED")
    return leaves, units, unit_rows


def index_snapshot(index, rows):
    from knowpath_backend.learning.rag.vector import point_id
    from knowpath_backend.rag_eval.dataset import digest
    index.verify(rows)
    ids = sorted(point_id(row) for row in rows)
    records = {}
    for start in range(0, len(ids), 256):
        for record in index.client.retrieve(index.collection, ids=ids[start:start + 256],
                                             with_vectors=True, with_payload=True):
            records[str(record.id)] = record
    fingerprint = digest([{ "id": identifier, "payload": records[identifier].payload,
                            "vector": records[identifier].vector} for identifier in ids])
    return fingerprint, {row["chunk_id"]: records[point_id(row)].vector for row in rows}


def verify_indexes(*, copy=False):
    from qdrant_client import QdrantClient
    from knowpath_backend.learning.providers.models import embedding_model
    from knowpath_backend.learning.rag.vector import QdrantContentIndex
    settings, _ = configure()
    embedder = embedding_model(settings)  # construction does not embed/call a model
    profile = dict(provider=settings.embedding_provider, model=embedder.model_version,
                   dimension=embedder.dimension, endpoint=settings.embedding_base_url)
    leaves, units, unit_rows = canonical_rows()
    with closing(QdrantClient(url="http://127.0.0.1:6333", api_key=None, timeout=60,
                              check_compatibility=False)) as client:
        fingerprints = {}
        leaf_vectors = None
        for suffix, rows in (("", leaves), ("_units", unit_rows)):
            target_index = QdrantContentIndex(client, PREFIX + suffix, embedder.dimension, profile)
            if copy:
                if client.collection_exists(target_index.collection):
                    raise ValueError("TARGET_INDEX_ALREADY_EXISTS")
                old_index = QdrantContentIndex(client, "knowpath_rag_b3_unit_40_v1" + suffix,
                                               embedder.dimension, profile)
                # Original frozen units predate structural payload binding.
                # Verify their exact old payload, then bind canonical metadata
                # only in the new isolated collection; original stays intact.
                legacy_rows = [{key: value for key, value in row.items()
                                if key not in {"source_map_hash", "leaf_ids", "anchor_id"}}
                               for row in rows] if suffix else rows
                _, vectors = index_snapshot(old_index, legacy_rows)
                target_index.upsert(rows, [vectors[row["chunk_id"]] for row in rows])
            fingerprint, vectors = index_snapshot(target_index, rows)
            fingerprints[suffix or "leaves"] = fingerprint
            if not suffix:
                leaf_vectors = vectors
            else:
                for unit in units:
                    members = [leaf_vectors[cid] for cid in unit.leaf_ids]
                    centroid = [sum(v[i] for v in members) / len(members) for i in range(embedder.dimension)]
                    norm = math.sqrt(sum(v * v for v in centroid))
                    if not norm or max(abs(a / norm - b) for a, b in zip(centroid, vectors[unit.unit_id])) > 1e-6:
                        raise ValueError("UNIT_CENTROID_CHANGED")
        snapshot = dict(collection_prefix=PREFIX, embedding_profile=profile,
                        representation="normalized-centroid-of-frozen-leaf-vectors-v1",
                        leaf_count=len(leaves), unit_count=len(units), fingerprints=fingerprints)
        if copy:
            write(TARGET / "index-snapshot.json", snapshot)
            write(TARGET / "unit-manifest.json", {"units": [asdict(unit) for unit in units]})
        elif snapshot != read(TARGET / "index-snapshot.json"):
            raise ValueError("FROZEN_INDEX_CHANGED")
    return snapshot


def prepare():
    if file_hash(SOURCE / "dataset.jsonl") != DATASET_HASH:
        raise ValueError("ORIGINAL_DATASET_CHANGED")
    TARGET.mkdir(exist_ok=False)
    for name in ("dataset.jsonl", "split.json", "rubric.md"):
        shutil.copyfile(SOURCE / name, TARGET / name)
    shutil.copytree(SOURCE / "sources", TARGET / "sources")
    with sqlite3.connect((SOURCE / "evaluation.db").as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(TARGET / "evaluation.db") as target:
            source.backup(target)
    receipt = verify_indexes(copy=True)
    finish_preparation(receipt)


def finish_preparation(receipt):
    """Write exclusive config files after local index preparation succeeds."""
    _, actual = configure()
    config = read(SOURCE / "development.json")
    config.update(actual)
    config.update(bind_manifests())
    config["repair_experiment"] = {
        "original_dataset_sha256": DATASET_HASH,
        "original_results_sha256": file_hash(SOURCE / "dev-v5.jsonl"),
        "input_budget_change": [12000, 14000],
        "index": receipt,
        "quality_review": "pending",
        "adoption_gates": {"context_coverage_gain": 0.03, "negative_control_regression": 0,
                           "p95_ratio": 1.2},
    }
    write(TARGET / "development.json", config)
    write(TARGET / "structure-audit.json", {
        "kind": "offline_diagnostic_not_runtime_input", "dataset_unchanged": True,
        "continuation_gold_question_ids": ["tree-v1-03", "tree-v1-05", "tree-v1-09", "tree-v1-10", "tree-v1-17"],
        "single_leaf_labels_requiring_cross_leaf_coverage": ["tree-v1-03", "tree-v1-10"],
    })
    print(json.dumps({"prepared": str(TARGET), "leaves": 970, "units": 934}), flush=True)


def bind_manifests():
    """Publish new verified manifests only inside the isolated database copy."""
    from copy import deepcopy
    from qdrant_client import QdrantClient
    from knowpath_backend.learning.persistence.db import create_db_engine
    from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
    from knowpath_backend.learning.persistence.rag_repository import SqlRagRepository
    from knowpath_backend.learning.state import LearningState
    from knowpath_backend.learning.providers.models import embedding_model
    from knowpath_backend.learning.rag.lifecycle import ManifestLifecycle
    from knowpath_backend.learning.rag.bm25 import BM25Index
    from knowpath_backend.learning.rag.registry import create_plugin
    from knowpath_backend.learning.rag.vector import QdrantContentIndex
    from knowpath_backend.rag_eval.dataset import digest
    settings, _ = configure()
    engine = create_db_engine()
    bindings = deepcopy(read(SOURCE / "development.json")["runtime_bindings"])
    configurations, pinned = {}, {}
    try:
        state = LearningState(SqlAlchemyMaterialRepository(engine))
        repo = SqlRagRepository(engine)
        lifecycle = ManifestLifecycle(repo)
        embedder = embedding_model(settings)
        profile = dict(provider=settings.embedding_provider, model=embedder.model_version,
                       dimension=embedder.dimension, endpoint=settings.embedding_base_url)
        with closing(QdrantClient(url="http://127.0.0.1:6333", api_key=None, timeout=60,
                                  check_compatibility=False)) as client:
            dense = QdrantContentIndex(client, PREFIX, embedder.dimension, profile)
            for space_id in sorted({binding["space_id"] for binding in bindings.values()}):
                scope = state.space_service.rag_scope_snapshot(space_id)
                for old in lifecycle.pin(scope, require_b1=True):
                    configuration = dict(old["configuration"], vector_collection=dense.collection,
                                         collection_prefix=PREFIX)
                    if configuration == old["configuration"]:
                        continue
                    mid = digest(["isolated-index-rebinding-v1", old["manifest_id"], configuration])
                    manifest = lifecycle.start(old["retrieval_version_id"], mid, scope,
                        old["expected_chunks"], configuration, tree_version_id=old["tree_version_id"])
                    def readback(current, chunks):
                        return {"dense": dense.verify(chunks),
                                "lexical": BM25Index(chunks, current["configuration"]["bm25"]).verify(chunks),
                                "tree": {"verified": True, "tree_version_id": current["tree_version_id"]}}
                    manifest = lifecycle.validate(old["retrieval_version_id"], mid, readback,
                                                  attempt=manifest["attempt"])
                    if not manifest["b1_ready"]:
                        raise ValueError("TREE_REBIND_VALIDATION_FAILED")
                    lifecycle.publish(old["retrieval_version_id"], mid, expected_generation=old["generation"])
                manifests = lifecycle.pin(scope, require_b1=True)
                for mode in ("a", "b2_r1", "b3_unit"):
                    create_plugin(mode, embedder, dense).validate_runtime(manifests)
                pinned[space_id] = manifests
            for binding in bindings.values():
                manifests = pinned[binding["space_id"]]
                binding["expected_manifest_ids"] = [m["manifest_id"] for m in manifests]
                binding["manifest_configuration_hashes"] = {
                    m["manifest_id"]: digest(m["configuration"]) for m in manifests}
                if sorted(binding["expected_retrieval_versions"]) != sorted(m["retrieval_version_id"] for m in manifests):
                    raise ValueError("RETRIEVAL_ID_CHANGED")
                configurations.update(binding["manifest_configuration_hashes"])
    finally:
        engine.dispose()
    return {"runtime_bindings": bindings, "profile": {"manifest_configuration_hashes": configurations}}


def repair_prepared_bindings():
    if (TARGET / "freeze.json").exists() or (TARGET / "dev.jsonl").exists():
        raise ValueError("EXPERIMENT_ALREADY_FROZEN")
    config = read(TARGET / "development.json")
    write(TARGET / "development-before-rebinding.json", config)
    config.update(bind_manifests())
    pending = TARGET / "development-rebound.json"
    write(pending, config)
    pending.replace(TARGET / "development.json")
    print(json.dumps({"isolated_manifest_profiles_verified": 3}), flush=True)


def preflight():
    """Read back vectors and exercise actual runtime bindings without model calls."""
    from qdrant_client import QdrantClient
    from knowpath_backend.rag_eval.cli import configured_runtime
    from knowpath_backend.rag_eval.runner import _check_scope
    from knowpath_backend.learning.providers.models import embedding_model
    from knowpath_backend.learning.rag.registry import create_plugin
    from knowpath_backend.learning.rag.pipeline import RagPipeline
    from knowpath_backend.learning.rag.units import build_units
    from knowpath_backend.learning.rag.vector import QdrantContentIndex
    receipt = verify_indexes()
    config = read(TARGET / "development.json")
    factory, resolver, engine = configured_runtime({"config": config})
    try:
        instance = factory("a")
        embedder = embedding_model(instance.settings)
        with closing(QdrantClient(url="http://127.0.0.1:6333", api_key=None, timeout=60,
                                  check_compatibility=False)) as client:
            dense = QdrantContentIndex(client, PREFIX, embedder.dimension, receipt["embedding_profile"])
            source_units = {unit["unit_id"]: unit for unit in read(TARGET / "unit-manifest.json")["units"]}
            for binding in {b["space_id"]: b for b in config["runtime_bindings"].values()}.values():
                actual = resolver(binding["space_id"])
                _check_scope(binding, actual)
                for mode in ("a", "b2_r1", "b3_unit"):
                    create_plugin(mode, embedder, dense).validate_runtime(actual["manifests"])
                pipeline = RagPipeline(instance.repo, instance.materials, instance.spaces, None, None, None)
                scope = instance.spaces.rag_scope_snapshot(binding["space_id"])
                rows = pipeline._originals(scope, actual["manifests"])
                dense.verify(rows)
                for unit in build_units(rows):
                    if unit.source_map_hash != source_units[unit.unit_id]["source_map_hash"]:
                        raise ValueError("RUNTIME_SOURCE_MAP_CHANGED")
    finally:
        engine.dispose()
    print(json.dumps({"preflight": "passed", "runtime_modes": 3, "scopes": 3}), flush=True)


def freeze_run():
    from knowpath_backend.rag_eval.dataset import freeze, digest, verify_freeze
    preflight()
    temporary = TARGET / "freeze-base.json"
    frozen = freeze(TARGET / "development.json", temporary)
    for path in [Path(__file__), TARGET / "unit-manifest.json", TARGET / "index-snapshot.json",
                 TARGET / "structure-audit.json"]:
        frozen["file_hashes"][str(path)] = file_hash(path)
    for name in ("validation.json", "offline-capacity-replay.json", "offline-capacity-replay-source.txt",
                 "scoring-spec-review.md", "scoring-quality-review.md", "repair-spec-review.md",
                 "repair-quality-review.md"):
        path = TARGET / name
        if path.exists():
            frozen["file_hashes"][str(path)] = file_hash(path)
    frozen["freeze_id"] = digest({k: v for k, v in frozen.items() if k != "freeze_id"})
    write(TARGET / "freeze.json", frozen)
    verify_freeze(TARGET / "freeze.json")
    with zipfile.ZipFile(TARGET / "frozen-code.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name in frozen["file_hashes"]:
            path = Path(name)
            if path.is_relative_to(PY) and not path.is_relative_to(TARGET):
                archive.write(path, path.relative_to(PY).as_posix())
    print(json.dumps({"freeze_id": frozen["freeze_id"], "scheduled_requests": 120}), flush=True)


def execute():
    from knowpath_backend.rag_eval.cli import configured_runtime
    from knowpath_backend.rag_eval.dataset import verify_freeze
    from knowpath_backend.rag_eval.runner import run
    configure()
    frozen, rows, split = verify_freeze(TARGET / "freeze.json")
    if len(rows) != 40 or len(split["dev"]["question_ids"]) != 40:
        raise ValueError("SCHEDULE_CHANGED")
    preflight()
    factory, resolver, engine = configured_runtime(frozen)
    try:
        records = run(TARGET / "freeze.json", TARGET / "dev.jsonl", partition="dev",
                      pipeline_factory=factory, scope_resolver=resolver)
    finally:
        engine.dispose()
    verify_freeze(TARGET / "freeze.json")
    verify_indexes()
    print(json.dumps({"attempted": len(records),
                      "service_successes": sum(r["service_success"] for r in records)}), flush=True)
    score()


def score():
    from knowpath_backend.rag_eval.scoring import report
    summary = report(TARGET / "freeze.json", TARGET / "dev.jsonl")
    write(TARGET / "report.json", summary)
    print(json.dumps({"decision": summary["decision"],
                      "missing_execution_rows": summary["missing_execution_rows"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "bind", "preflight", "freeze", "run", "report"))
    args = parser.parse_args()
    try:
        {"prepare": prepare, "bind": repair_prepared_bindings, "preflight": preflight, "freeze": freeze_run,
         "run": execute, "report": score}[args.stage]()
    except Exception as error:
        # Never echo endpoint errors, DSNs, provider bodies or credentials.
        print(json.dumps({"stage": args.stage, "error_type": type(error).__name__,
                          "status": "experiment_stopped"}), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
