"""Model-bound input accounting and whole-evidence protocol capacity planning.

Byte accounting is an intentionally conservative operational bound, conditional
on byte-level tokenization and the configured server-template allowance. It is
not a measured token count or proof about an undocumented remote chat template.
No approximate byte/token conversion is used to admit a request.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Callable, Mapping, Protocol


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _validate_request(body, model):
    if not isinstance(body, dict) or body.get("model") != model:
        raise ValueError("token profile model mismatch")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or any(
        not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant"}
        or not isinstance(m.get("content"), str) for m in messages
    ):
        raise ValueError("text chat messages required")
    _serialize(body)


@dataclass(frozen=True)
class InputCount:
    value: int
    kind: str
    unit: str
    profile_id: str
    estimated_tokens: int | None = None
    upper_bound_tokens: int | None = None

    def __post_init__(self):
        _integer(self.value, "input count")


class CountingProfile(Protocol):
    provider: str
    model: str
    profile_id: str

    def count_request(self, body: dict) -> InputCount: ...


@dataclass(frozen=True)
class ConservativeByteProfile:
    provider: str
    model: str
    per_message_overhead: int = 32
    request_overhead: int = 64

    def __post_init__(self):
        if not self.provider or not self.model:
            raise ValueError("provider and deployment model are required")
        _integer(self.per_message_overhead, "chat message overhead")
        _integer(self.request_overhead, "chat request overhead")

    @property
    def profile_id(self):
        return (f"utf8-envelope-v1:{self.provider}:{self.model}:"
                f"message={self.per_message_overhead}:request={self.request_overhead}")

    @property
    def provenance(self):
        return {"kind": "conservative_upper_bound", "provider": self.provider, "model": self.model,
                "profile_id": self.profile_id, "tokenizer_revision": None, "artifact_sha256": None,
                "chat_template": "unverified_server_template_allowance",
                "per_message_overhead": self.per_message_overhead, "request_overhead": self.request_overhead}

    def count_request(self, body):
        _validate_request(body, self.model)
        value = (len(_serialize(body)) + len(body["messages"]) * self.per_message_overhead
                 + self.request_overhead)
        return InputCount(value, "conservative_upper_bound", "utf8_bytes_plus_token_reserve",
                          self.profile_id, upper_bound_tokens=value)

    def count_messages(self, messages, *, request_options=None):
        options = dict(request_options or {})
        if {"model", "messages"} & options.keys():
            raise ValueError("request options cannot override profile identity or messages")
        return self.count_request({"model": self.model, "messages": messages, **options})


def _load_local_tokenizer(directory):
    try:
        from transformers import AutoTokenizer
    except ImportError:
        raise ValueError("verified local token counting requires the optional transformers package") from None
    return AutoTokenizer.from_pretrained(str(directory), local_files_only=True,
                                         trust_remote_code=False, use_fast=True)


@dataclass(frozen=True)
class VerifiedLocalProfile:
    """Locally verified artifacts; deployment equivalence remains an operator attestation.

    Use load_verified_local_profile to validate before construction. Counts are
    still estimates of server usage, because provider wrappers can differ.
    """
    provider: str
    model: str
    _tokenizer: object
    _manifest: dict
    _manifest_sha256: str

    @property
    def profile_id(self):
        return f"verified-local-v1:{self.provider}:{self.model}:{self._manifest_sha256}"

    @property
    def provenance(self):
        return {**deepcopy(self._manifest), "manifest_sha256": self._manifest_sha256,
                "profile_id": self.profile_id, "kind": "tokenizer_estimate"}

    def count_request(self, body):
        _validate_request(body, self.model)
        allowed = {"model", "messages", "max_tokens", "response_format", "enable_thinking",
                   "stream", "temperature", "top_p"}
        if (set(body) - allowed or any(set(message) != {"role", "content"} for message in body["messages"])
                or body.get("enable_thinking") is not False
                or body.get("stream", False) is not False
                or body.get("response_format") != {"type": "json_object"}):
            raise ValueError("request capabilities do not match the reviewed text JSON profile")
        ids = self._tokenizer.apply_chat_template(deepcopy(body["messages"]), tokenize=True,
            add_generation_prompt=True, **self._manifest["chat_template_kwargs"])
        if not isinstance(ids, list) or any(type(token) is not int or token < 0 for token in ids):
            raise ValueError("local tokenizer returned invalid token IDs")
        # The template covers roles/content/control tokens. The separate JSON
        # envelope allowance conservatively includes provider request options.
        envelope = {k: v for k, v in body.items() if k != "messages"}
        value = len(ids) + len(_serialize(envelope)) + self._manifest["safety_margin_tokens"]
        return InputCount(value, "tokenizer_estimate", "tokens_plus_envelope_reserve", self.profile_id,
                          estimated_tokens=len(ids))

    def count_messages(self, messages, *, request_options=None):
        options = dict(request_options or {})
        if {"model", "messages"} & options.keys():
            raise ValueError("request options cannot override profile identity or messages")
        return self.count_request({"model": self.model, "messages": messages, **options})


def load_verified_local_profile(manifest_path, *, expected_manifest_sha256, provider, model):
    """Load a separately reviewed, SHA-pinned local tokenizer; never download one.

    The manifest is an attestation, not proof that an open Qwen tokenizer matches
    a hosted alias. An operator must obtain deployment-match evidence before
    supplying this digest. Artifact digests detect drift, not model equivalence.
    """
    path = Path(manifest_path).resolve(strict=True)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_manifest_sha256:
        raise ValueError("token profile manifest digest mismatch")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
            or manifest["version"] != 1 or manifest.get("provider") != provider
            or manifest.get("model") != model):
        raise ValueError("token profile deployment binding mismatch")
    for name in ("model_revision", "artifact_revision", "source_url", "deployment_match_evidence"):
        if not isinstance(manifest.get(name), str) or not manifest[name].strip():
            raise ValueError("tokenizer deployment provenance is incomplete")
    if not manifest["source_url"].startswith("https://"):
        raise ValueError("tokenizer source provenance requires HTTPS")
    if not re.fullmatch(r"[0-9a-f]{40,64}", manifest["artifact_revision"]):
        raise ValueError("immutable artifact commit revision required")
    if manifest.get("chat_template_kwargs") != {"enable_thinking": False}:
        raise ValueError("only reviewed nonthinking text-chat templates are supported")
    _integer(manifest.get("safety_margin_tokens"), "tokenizer safety margin", 1)
    files = manifest.get("files")
    if not isinstance(files, dict) or not {"tokenizer.json", "tokenizer_config.json"} <= files.keys():
        raise ValueError("complete local tokenizer artifact manifest required")
    root, verified = path.parent, set()
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise ValueError("invalid tokenizer artifact manifest")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("tokenizer artifact path escapes profile directory")
        artifact = (root / relative).resolve(strict=True)
        if not artifact.is_relative_to(root) or artifact == path or artifact in verified:
            raise ValueError("invalid tokenizer artifact path")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
            raise ValueError("tokenizer artifact digest mismatch")
        verified.add(artifact)
    if {p.resolve() for p in root.rglob("*") if p.is_file() and p.resolve() != path} != verified:
        raise ValueError("unreviewed tokenizer artifact file")
    config = json.loads((root / "tokenizer_config.json").read_text(encoding="utf-8"))
    template = config.get("chat_template")
    if (not isinstance(template, str) or not template
            or hashlib.sha256(template.encode("utf-8")).hexdigest() != manifest.get("chat_template_sha256")):
        raise ValueError("chat template digest mismatch")
    tokenizer = _load_local_tokenizer(root)
    if getattr(tokenizer, "chat_template", None) != template:
        raise ValueError("loaded tokenizer chat template mismatch")
    return VerifiedLocalProfile(provider, model, tokenizer, deepcopy(manifest), digest)


@dataclass(frozen=True)
class UsageCalibration:
    input_count: InputCount
    actual_prompt_tokens: int | None
    actual_completion_tokens: int | None
    within_upper_bound: bool | None


def calibrate_usage(count, usage):
    """Missing provider counters remain unknown, including partially billed failures."""
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object")
    for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens"):
        if key in usage:
            _integer(usage[key], "provider usage")
    for primary, alias in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        if primary in usage and alias in usage and usage[primary] != usage[alias]:
            raise ValueError("conflicting provider usage counters")
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    within = None if prompt is None or count.upper_bound_tokens is None else prompt <= count.upper_bound_tokens
    return UsageCalibration(count, prompt, completion, within)


class TokenBudgetExceeded(ValueError):
    def __init__(self, reason, *, stage=None):
        super().__init__("MODEL_TOKEN_BUDGET_EXCEEDED")
        self.reason = reason
        self.stage = stage


@dataclass(frozen=True)
class ModelBudget:
    application_input_limit: int
    context_window_tokens: int
    output_limit_tokens: int

    def __post_init__(self):
        for name in ("application_input_limit", "context_window_tokens", "output_limit_tokens"):
            _integer(getattr(self, name), name, 1)

    def check(self, count, *, reserved_input_tokens=0, stage=None):
        _integer(reserved_input_tokens, "future input reserve")
        total = count.value + reserved_input_tokens
        if total > self.application_input_limit:
            raise TokenBudgetExceeded("application_input_limit", stage=stage)
        if total + self.output_limit_tokens > self.context_window_tokens:
            raise TokenBudgetExceeded("context_window", stage=stage)


@dataclass(frozen=True)
class StageRequest:
    stage: str
    body: dict
    reserved_input_tokens: int = 0

    def __post_init__(self):
        if not isinstance(self.stage, str) or not self.stage:
            raise ValueError("stage identity required")
        _integer(self.reserved_input_tokens, "future input reserve")


@dataclass(frozen=True)
class PackedEvidence:
    rows: list[dict]
    stage_counts: Mapping[str, InputCount]
    omitted_chunk_ids: tuple[str, ...]
    invalid_chunk_ids: tuple[str, ...]


def pack_evidence_groups(rows, *, build_requests: Callable, profile: CountingProfile,
                         budgets: Mapping[str, ModelBudget]):
    """Greedy stable packing against the complete generation/check/revision protocol.

    build_requests receives complete selected rows and must return every planned
    stage with its real envelope and worst-case future-input reserve. Reserves
    must include serialization/duplication expansion, not merely the preceding
    output max_tokens. They are not measured usage. Each stage reserves its own
    output separately through ModelBudget. All inputs and results are copied.
    """
    rows = deepcopy(list(rows))
    by_id = {r["chunk_id"]: r for r in rows}
    if len(by_id) != len(rows):
        raise ValueError("duplicate context identity")
    if not budgets:
        raise ValueError("at least one stage budget required")

    def measure(selected):
        requests = list(build_requests(deepcopy(selected)))
        names = [r.stage for r in requests]
        if len(set(names)) != len(names) or set(names) != set(budgets):
            raise ValueError("complete unique stage budgets required")
        counts = {}
        for stage in requests:
            budget = budgets[stage.stage]
            if stage.body.get("max_tokens") != budget.output_limit_tokens:
                raise ValueError("request output limit differs from stage reservation")
            count = profile.count_request(stage.body)
            budget.check(count, reserved_input_tokens=stage.reserved_input_tokens, stage=stage.stage)
            counts[stage.stage] = count
        return counts

    counts = measure([])  # Fixed-protocol overflow is an error even with no evidence.
    groups = {}
    for row in rows:
        if row.get("evidence_group"):
            groups.setdefault(row["evidence_group"], set()).add(row["chunk_id"])
    selected, seen, invalid, capacity_omitted = [], set(), set(), False
    for row in rows:
        if row["chunk_id"] in seen:
            continue
        pending, closure, missing = [row["chunk_id"]], set(), False
        while pending:
            identifier = pending.pop()
            if identifier in closure:
                continue
            closure.add(identifier)
            item = by_id.get(identifier)
            if item is None:
                missing = True
                break
            pending.extend(item.get("requires", []))
            pending.extend(groups.get(item.get("evidence_group"), set()) - closure)
        if missing:
            invalid.add(row["chunk_id"])
            continue
        candidate = selected + [r for r in rows if r["chunk_id"] in closure - seen]
        try:
            candidate_counts = measure(candidate)
        except TokenBudgetExceeded:
            capacity_omitted = True
            continue
        selected, counts = candidate, candidate_counts
        seen.update(closure)
    if rows and not selected and capacity_omitted:
        raise TokenBudgetExceeded("no_complete_evidence_group_fits")
    return PackedEvidence(deepcopy(selected), counts,
                          tuple(r["chunk_id"] for r in rows if r["chunk_id"] not in seen),
                          tuple(r["chunk_id"] for r in rows if r["chunk_id"] in invalid))
