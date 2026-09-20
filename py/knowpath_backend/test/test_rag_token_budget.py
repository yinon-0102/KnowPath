"""Offline request accounting and dependency-preserving capacity planning."""
import importlib
import importlib.util
import json
import hashlib
from copy import deepcopy

import pytest


def test_model_aware_budget_module_exists():
    assert importlib.util.find_spec("knowpath_backend.learning.rag.token_budget") is not None


@pytest.fixture
def api():
    name = "knowpath_backend.learning.rag.token_budget"
    assert importlib.util.find_spec(name) is not None, "model-aware budget module is missing"
    return importlib.import_module(name)


def request(text="问题", **extra):
    return {"model": "qwen-plus", "messages": [{"role": "user", "content": text}],
            "max_tokens": 100, "response_format": {"type": "json_object"},
            "enable_thinking": False, **extra}


def test_byte_profile_counts_full_request_and_chat_overhead_without_claiming_exact(api):
    profile = api.ConservativeByteProfile(provider="dashscope", model="qwen-plus")
    body = request()
    count = profile.count_request(body)
    raw = len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
    assert count.value >= raw + profile.per_message_overhead + profile.request_overhead
    assert count.kind == "conservative_upper_bound"
    assert count.unit == "utf8_bytes_plus_token_reserve"
    assert count.estimated_tokens is None
    assert count.upper_bound_tokens == count.value
    assert count.profile_id == profile.profile_id
    assert profile.count_messages(body["messages"], request_options={k: v for k, v in body.items()
        if k not in {"model", "messages"}}) == count
    assert profile.count_request(request(response_format={"type": "json_schema", "schema": {"large": "X" * 200}})).value > count.value


@pytest.mark.parametrize("body", [request(model="qwen-turbo"), request(messages=[]),
    request(messages=[{"role": "user", "content": ["multimodal"]}]), request(temperature=float("nan"))])
def test_profiles_reject_wrong_model_and_invalid_request(api, body):
    with pytest.raises(ValueError):
        api.ConservativeByteProfile(provider="dashscope", model="qwen-plus").count_request(body)


def test_actual_usage_is_separate_and_unknown_is_not_zero(api):
    profile = api.ConservativeByteProfile(provider="dashscope", model="qwen-plus")
    count = profile.count_request(request())
    assert api.calibrate_usage(count, None).actual_prompt_tokens is None
    calibration = api.calibrate_usage(count, {"prompt_tokens": 30, "completion_tokens": 7})
    assert calibration.actual_prompt_tokens == 30
    assert calibration.actual_completion_tokens == 7
    assert calibration.within_upper_bound is True
    assert api.calibrate_usage(count, {"input_tokens": count.value + 1}).within_upper_bound is False
    with pytest.raises(ValueError):
        api.calibrate_usage(count, {"prompt_tokens": True})


def test_application_context_and_output_limits_are_independent(api):
    profile = api.ConservativeByteProfile(provider="dashscope", model="qwen-plus")
    count = profile.count_request(request())
    api.ModelBudget(count.value, count.value + 100, 100).check(count)
    with pytest.raises(api.TokenBudgetExceeded) as error:
        api.ModelBudget(count.value - 1, count.value + 1000, 100).check(count)
    assert error.value.reason == "application_input_limit"
    with pytest.raises(api.TokenBudgetExceeded) as error:
        api.ModelBudget(count.value + 1000, count.value + 99, 100).check(count)
    assert error.value.reason == "context_window"


def test_packing_counts_all_stage_envelopes_and_reserves_whole_dependency_groups(api):
    profile = api.ConservativeByteProfile(provider="dashscope", model="qwen-plus")
    rows = [{"chunk_id": "a", "source_text": "主体", "requires": ["b"]},
            {"chunk_id": "b", "source_text": "必要条件", "evidence_group": "g"},
            {"chunk_id": "c", "source_text": "例外", "evidence_group": "g"},
            {"chunk_id": "d", "source_text": "X" * 1000}]
    original = deepcopy(rows)
    def build(selected):
        text = "\n".join(r["source_text"] for r in selected)
        return [api.StageRequest("generate", request(text)),
                api.StageRequest("check", request(text), reserved_input_tokens=150),
                api.StageRequest("revise", request(text), reserved_input_tokens=250)]
    limit = profile.count_request(request("主体\n必要条件\n例外")).value + 250
    budgets = {name: api.ModelBudget(limit, limit + 100, 100) for name in ["generate", "check", "revise"]}
    result = api.pack_evidence_groups(rows, build_requests=build, profile=profile, budgets=budgets)
    assert [r["chunk_id"] for r in result.rows] == ["a", "b", "c"]
    assert result.omitted_chunk_ids == ("d",)
    assert result.stage_counts["revise"].value + 250 == limit
    result.rows[0]["source_text"] = "changed"
    assert rows == original


def test_fixed_protocol_overflow_and_no_fitting_evidence_are_explicit(api):
    profile = api.ConservativeByteProfile(provider="dashscope", model="qwen-plus")
    def build(rows):
        return [api.StageRequest("check", request("".join(r["source_text"] for r in rows)))]
    fixed = profile.count_request(request("")).value
    with pytest.raises(api.TokenBudgetExceeded) as error:
        api.pack_evidence_groups([], build_requests=build, profile=profile,
            budgets={"check": api.ModelBudget(fixed - 1, 10000, 100)})
    assert error.value.stage == "check"
    with pytest.raises(api.TokenBudgetExceeded) as error:
        api.pack_evidence_groups([{"chunk_id": "a", "source_text": "不可裁剪"}],
            build_requests=build, profile=profile, budgets={"check": api.ModelBudget(fixed, 10000, 100)})
    assert error.value.reason == "no_complete_evidence_group_fits"


def test_missing_dependency_is_not_relabelled_budget_failure(api):
    profile = api.ConservativeByteProfile(provider="dashscope", model="qwen-plus")
    result = api.pack_evidence_groups([{"chunk_id": "a", "source_text": "x", "requires": ["missing"]}],
        build_requests=lambda rows: [api.StageRequest("generate", request())], profile=profile,
        budgets={"generate": api.ModelBudget(10000, 11000, 100)})
    assert result.rows == []
    assert result.invalid_chunk_ids == ("a",)


def test_packing_rejects_missing_or_duplicate_stage_contract(api):
    profile = api.ConservativeByteProfile(provider="dashscope", model="qwen-plus")
    for stages in [[], [api.StageRequest("other", request())],
                   [api.StageRequest("check", request()), api.StageRequest("check", request())]]:
        with pytest.raises(ValueError):
            api.pack_evidence_groups([], build_requests=lambda rows: stages, profile=profile,
                budgets={"check": api.ModelBudget(10000, 11000, 100)})


def local_manifest(tmp_path):
    template = "pinned chat template with assistant generation marker"
    files = {"tokenizer.json": "{}", "tokenizer_config.json": json.dumps({"chat_template": template})}
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    manifest = {"version": 1, "provider": "dashscope", "model": "qwen-plus",
        "model_revision": "reviewed-deployment-2026-09-21", "artifact_revision": "a" * 40,
        "source_url": "https://example.invalid/test-only-tokenizer",
        "deployment_match_evidence": "test-only reviewed provider deployment mapping",
        "files": {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in files},
        "chat_template_sha256": hashlib.sha256(template.encode()).hexdigest(),
        "chat_template_kwargs": {"enable_thinking": False}, "safety_margin_tokens": 64}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), manifest, template


def test_local_profile_is_pinned_bound_and_still_distinguished_from_server_usage(api, tmp_path, monkeypatch):
    path, digest, manifest, template = local_manifest(tmp_path)
    calls = []
    class Tokenizer:
        chat_template = template
        def apply_chat_template(self, messages, **kwargs):
            calls.append((deepcopy(messages), kwargs))
            return [10, 11, 12, 13]
    monkeypatch.setattr(api, "_load_local_tokenizer", lambda directory: Tokenizer())
    profile = api.load_verified_local_profile(path, expected_manifest_sha256=digest,
        provider="dashscope", model="qwen-plus")
    count = profile.count_request(request("汉字" * 200))
    assert count.kind == "tokenizer_estimate"
    assert count.estimated_tokens == 4
    assert count.upper_bound_tokens is None
    assert count.value > count.estimated_tokens
    assert count.value < api.ConservativeByteProfile("dashscope", "qwen-plus").count_request(request("汉字" * 200)).value
    assert calls[0][1] == {"tokenize": True, "add_generation_prompt": True, "enable_thinking": False}
    assert profile.provenance["manifest_sha256"] == digest
    assert profile.provenance["artifact_revision"] == manifest["artifact_revision"]
    assert api.calibrate_usage(count, {"prompt_tokens": 8}).within_upper_bound is None
    with pytest.raises(ValueError):
        profile.count_request(request(enable_thinking=True))
    with pytest.raises(ValueError):
        profile.count_request(request(tools=[{"type": "function"}]))


@pytest.mark.parametrize("mutation", ["manifest_digest", "artifact_digest", "extra_file", "path_escape", "provider", "model", "chat_template", "missing_evidence"])
def test_local_profile_refuses_unverified_bindings_before_loading(api, tmp_path, monkeypatch, mutation):
    path, digest, manifest, template = local_manifest(tmp_path)
    loads = []
    monkeypatch.setattr(api, "_load_local_tokenizer", lambda directory: loads.append(directory))
    provider, model = "dashscope", "qwen-plus"
    if mutation == "manifest_digest":
        digest = "0" * 64
    elif mutation == "artifact_digest":
        (tmp_path / "tokenizer.json").write_text("changed", encoding="utf-8")
    elif mutation == "extra_file":
        (tmp_path / "unreviewed.json").write_text("{}", encoding="utf-8")
    elif mutation == "provider":
        provider = "openai"
    elif mutation == "model":
        model = "qwen-turbo"
    else:
        if mutation == "path_escape":
            manifest["files"]["../outside.json"] = "0" * 64
        elif mutation == "chat_template":
            manifest["chat_template_sha256"] = "0" * 64
        else:
            manifest["deployment_match_evidence"] = ""
        path.write_text(json.dumps(manifest), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        api.load_verified_local_profile(path, expected_manifest_sha256=digest, provider=provider, model=model)
    assert loads == []


def test_local_loader_never_downloads_or_executes_remote_tokenizer_code(api, tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    calls = []
    sentinel = object()
    class AutoTokenizer:
        @staticmethod
        def from_pretrained(directory, **kwargs):
            calls.append((directory, kwargs))
            return sentinel
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=AutoTokenizer))
    assert api._load_local_tokenizer(tmp_path) is sentinel
    assert calls == [(str(tmp_path), {"local_files_only": True, "trust_remote_code": False, "use_fast": True})]
