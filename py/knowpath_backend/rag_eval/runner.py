"""Append-only paired execution with allowlisted runtime inputs and no retries."""
from copy import deepcopy
import json
from pathlib import Path
import time

from .dataset import authorize_partition, digest, numeric, verify_freeze


def _bindings(values):
    return sorted((item["material_id"], item["material_version_id"], item["graph_version"]) for item in values)


def runtime_binding(config, row):
    mapping = config["runtime_bindings"]
    value = mapping.get(row["question_id"], mapping.get(row["scope_snapshot_id"]))
    required = {"space_id", "scope_snapshot_id", "expected_scope_version", "expected_bindings",
                "expected_manifest_ids", "expected_retrieval_versions", "manifest_configuration_hashes"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("each question requires an explicit real runtime scope/manifest mapping")
    if (not value["expected_manifest_ids"] or not value["expected_retrieval_versions"]
            or set(value["manifest_configuration_hashes"]) != set(value["expected_manifest_ids"])):
        raise ValueError("runtime mapping must freeze manifest and retrieval identities plus configuration hashes")
    return deepcopy(value)


def _check_scope(binding, actual):
    if hasattr(actual, "model_dump"):
        actual = actual.model_dump(mode="json")
    if (actual["space_id"] != binding["space_id"] or actual["scope_snapshot_id"] != binding["scope_snapshot_id"]
            or actual["scope_version"] != binding["expected_scope_version"]
            or _bindings(actual["bindings"]) != _bindings(binding["expected_bindings"])):
        raise ValueError("runtime scope changed")
    manifests = actual["manifests"]
    if (sorted(m["manifest_id"] for m in manifests) != sorted(binding["expected_manifest_ids"])
            or sorted(m["retrieval_version_id"] for m in manifests) != sorted(binding["expected_retrieval_versions"])
            or any(digest(m["configuration"]) != binding["manifest_configuration_hashes"][m["manifest_id"]] for m in manifests)):
        raise ValueError("runtime manifests/profile changed")


def _check_trace(binding, trace):
    if (trace.get("scope_snapshot_id") != binding["scope_snapshot_id"]
            or sorted(trace.get("manifest_ids", [])) != sorted(binding["expected_manifest_ids"])
            or sorted(trace.get("retrieval_versions", [])) != sorted(binding["expected_retrieval_versions"])):
        raise ValueError("runtime response did not use the frozen manifest set")


def run(freeze_path, output_path, *, partition="dev", pipeline_factory, scope_resolver):
    frozen, rows, split = verify_freeze(freeze_path)
    config = frozen["config"]
    selected = authorize_partition(frozen, rows, split, partition)
    bindings = {row["question_id"]: runtime_binding(config, row) for row in selected}
    records = []
    # Never overwrite or silently resume an earlier experiment; interruptions
    # leave their rows intact and scoring supplies missing scheduled denominators.
    with Path(output_path).open("x", encoding="utf-8") as stream:
        for question_index, row in enumerate(selected):
            binding = bindings[row["question_id"]]
            for repeat in range(config["repeats"]):
                order = ("a", "b1") if (question_index * config["repeats"] + repeat) % 2 == 0 else ("b1", "a")
                for position, mode in enumerate(order):
                    started = time.monotonic()
                    record = dict(freeze_id=frozen["freeze_id"], partition=partition,
                        question_id=row["question_id"], family_id=row["family_id"], plugin=mode, repeat=repeat,
                        order=position, service_success=False, error_code=None, error_details={}, answer_status=None,
                        response=None, cost=None, currency=config["pricing"].get("currency"),
                        runtime_binding=binding, evidence_kind="injected_harness" if not getattr(pipeline_factory, "real_runtime", False) else "service_integration")
                    pipeline = None
                    try:
                        _check_scope(binding, scope_resolver(binding["space_id"]))
                        pipeline = pipeline_factory(mode)
                        # Gold labels, expected statuses, evidence and family metadata
                        # cannot cross this explicit allowlist.
                        history = [{"role": item["role"], "content": item["content"]}
                                   for item in row.get("conversation_history", [])]
                        result = pipeline.answer(row["question"], space_id=binding["space_id"],
                            expected_scope_version=binding["expected_scope_version"],
                            expected_bindings=deepcopy(binding["expected_bindings"]), history=history)
                        _check_scope(binding, scope_resolver(binding["space_id"]))
                        _check_trace(binding, result["trace"])
                        status = result.get("status")
                        if status not in {"answered", "partial", "insufficient", "clarify", "failed"}:
                            raise ValueError("unknown answer status")
                        record.update(service_success=status != "failed", answer_status=status, response=result)
                        if status == "failed":
                            record["error_code"] = "PIPELINE_FAILED"
                        usage_cost = result["trace"].get("cost")
                        if usage_cost is None:
                            from .costing import estimate
                            usage_cost=estimate(result['trace'],config['pricing'])
                            if usage_cost is not None: result['trace']['cost']=usage_cost
                        if (isinstance(usage_cost, dict) and usage_cost.get("complete") is True
                                and usage_cost.get("currency") == record["currency"]
                                and numeric(usage_cost.get("amount"))):
                            record["cost"] = usage_cost["amount"]
                        # Ensure invalid provider objects cannot break append logging.
                        json.dumps(record, allow_nan=False)
                    except Exception as error:
                        from knowpath_backend.learning.rag.verification import VerificationError, safe_error_details
                        from knowpath_backend.learning.rag.diagnostics import safe_journal_snapshot
                        from knowpath_backend.learning.rag.retrieval import RetrievalError
                        code = error.code if isinstance(error, (VerificationError, RetrievalError)) else 'EVALUATION_REQUEST_FAILED'
                        details = safe_error_details(error.details) if isinstance(error,VerificationError) else {}
                        record.update(service_success=False, error_code=code, error_details=details, response=None)
                        record['call_journal'] = safe_journal_snapshot(getattr(error, 'call_journal', None))
                    finally:
                        if pipeline is not None and hasattr(pipeline, "close"):
                            try: pipeline.close()
                            except Exception: pass
                    record["latency_ms"] = (time.monotonic() - started) * 1000
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
                    records.append(record)
    return records
