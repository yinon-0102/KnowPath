"""Freeze exact experiment inputs; gold labels stay offline in this module."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path


GATES = ("max_p95_latency_ms", "max_mean_cost_per_request", "max_b1_latency_ratio", "max_b1_cost_ratio")


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    def constant(_): raise ValueError("nonfinite JSON")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=constant)


def numeric(value, *, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def _rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _validate_split(rows, split):
    by_id = {row["question_id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("duplicate question identity")
    questions, families = set(), set()
    for partition in ("dev", "heldout"):
        section = split[partition]
        ids, groups = section["question_ids"], section["family_ids"]
        if (len(set(ids)) != len(ids) or len(set(groups)) != len(groups)
                or questions.intersection(ids) or families.intersection(groups)):
            raise ValueError("question/family leakage across partitions")
        if not set(ids) <= by_id.keys() or {by_id[i]["family_id"] for i in ids} != set(groups):
            raise ValueError("question/family split mismatch")
        questions.update(ids)
        families.update(groups)
    if questions != set(by_id):
        raise ValueError("split must cover all dataset questions")
    return by_id


def _source_paths(value):
    if isinstance(value, dict):
        if "artifact_path" in value:
            yield value
        for child in value.values():
            yield from _source_paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from _source_paths(child)


def _validate_config(config):
    required = {"dataset", "split", "rubric", "human_review_status", "repeats", "order_schedule", "retries",
                "profile", "models", "prompts", "budgets", "statistics", "gates", "pricing", "decision_rule", "runtime_bindings"}
    if not required <= config.keys():
        raise ValueError("missing required freeze configuration fields")
    if type(config["repeats"]) is not int or not 1 <= config["repeats"] <= 100:
        raise ValueError("repeats must be between 1 and 100")
    if config["order_schedule"] != "alternating" or type(config["retries"]) is not int or config["retries"] != 0:
        raise ValueError("only alternating order with zero harness retries is supported")
    for name in ("profile", "models", "prompts", "budgets"):
        if not isinstance(config[name], dict) or not config[name]:
            raise ValueError(f"freeze must declare {name}")
    statistics = config["statistics"]
    supported = dict(method="family_descriptive", estimand="full_task_success_difference", weighting="equal_family",
                     repeat_aggregation="question_mean", failures="zero", quantile="nearest_rank")
    if any(statistics.get(k) != v for k, v in supported.items()) or type(statistics.get("seed")) is not int:
        raise ValueError("unsupported statistics; declare the implemented family descriptive method")
    if any(key not in config["gates"] for key in GATES):
        raise ValueError("declare every numerical gate, using null while undecided")
    if config["decision_rule"] != "exploratory_no_adoption":
        raise ValueError("this harness supports exploratory reporting, not automatic adoption")


def freeze(config_path, output_path):
    config_path, output_path = Path(config_path).resolve(), Path(output_path)
    config = read_json(config_path)
    _validate_config(config)
    paths = {name: (config_path.parent / config[name]).resolve() for name in ("dataset", "split", "rubric")}
    rows, split = _rows(paths["dataset"]), read_json(paths["split"])
    _validate_split(rows, split)
    files = {str(config_path), *(str(path) for path in paths.values())}
    root = paths["dataset"].parent
    for reference in _source_paths(rows):
        source = (root / reference["artifact_path"]).resolve()
        if not source.is_relative_to(root):
            raise ValueError("source path escapes dataset directory")
        raw = source.read_bytes()
        if sha256(raw).hexdigest() != reference["artifact_hash"]:
            raise ValueError("source artifact hash mismatch")
        if "quote" in reference and raw.decode("utf-8")[reference["start"]:reference["end"]] != reference["quote"]:
            raise ValueError("source span quote mismatch")
        files.add(str(source))
    # Freeze actual prompt/runtime implementation, not only user-supplied labels.
    backend = Path(__file__).resolve().parents[1]
    # Scope, persistence and publication guards are part of the executed
    # implementation too; freezing only retrieval/prompt modules is incomplete.
    files.update(str(path.resolve()) for path in (backend / "learning").rglob("*.py"))
    files.update(str(path.resolve()) for path in Path(__file__).parent.glob("*.py"))
    for name in ('pyproject.toml', 'uv.lock'):
        dependency_file = backend.parent / name
        if dependency_file.exists():
            files.add(str(dependency_file.resolve()))
    payload = dict(schema_version=1, created_at=datetime.now(timezone.utc).isoformat(),
                   config=config, paths={key: str(value) for key, value in paths.items()},
                   file_hashes={name: sha256(Path(name).read_bytes()).hexdigest() for name in sorted(files)})
    payload["freeze_id"] = digest(payload)
    with output_path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return payload


def verify_freeze(path):
    frozen = read_json(path)
    if frozen.get("freeze_id") != digest({k: v for k, v in frozen.items() if k != "freeze_id"}):
        raise ValueError("freeze hash mismatch")
    for name, expected in frozen["file_hashes"].items():
        if sha256(Path(name).read_bytes()).hexdigest() != expected:
            raise ValueError("frozen file hash mismatch")
    _validate_config(frozen["config"])
    rows, split = _rows(frozen["paths"]["dataset"]), read_json(frozen["paths"]["split"])
    _validate_split(rows, split)
    return frozen, rows, split


def authorize_partition(frozen, rows, split, partition):
    if partition not in {"dev", "heldout"}:
        raise ValueError("partition must be dev or heldout")
    if partition == "heldout":
        config = frozen["config"]
        if (config["human_review_status"] != "approved" or split.get("human_review_status") != "approved"
                or any(row.get("human_review_status") != "approved" for row in rows)):
            raise ValueError("heldout requires approved human review for config, split and every question")
        if any(not numeric(config["gates"].get(key), positive=True) for key in GATES):
            raise ValueError("heldout requires all four numeric cost/latency gates")
        pricing = config["pricing"]
        if not pricing.get("currency") or not pricing.get("date") or not pricing.get("price_table"):
            raise ValueError("heldout requires frozen pricing currency, date and price table")
    return [row for row in rows if row["question_id"] in split[partition]["question_ids"]]
