"""Managed contract facade for ordinary and structural retrieval implementations.

Scope/source resolution and erasure are trusted application callbacks. The facade
does not invent a second authorization policy or return plugin-provided text.
"""
from collections import OrderedDict
from copy import deepcopy
import time
from uuid import uuid4

from .contracts import Candidate, RetrievalResult
from .plugins import OrdinaryPlugin
from .tree import TreePlugin
from .section import SectionPlugin
from .continuation import ContinuationClosurePlugin
from .unit_first import UnitFirstPlugin
from .tree_navigation import TreeNavigationPlugin
from .flat_navigation import FlatNavigationPlugin
from .retrieval import RetrievalError
from .verification import VerificationError


REGISTRY = {"a": OrdinaryPlugin, "a_large": OrdinaryPlugin, "b1": TreePlugin, "b15": SectionPlugin,
            "b2_r1": ContinuationClosurePlugin, "b3_unit": UnitFirstPlugin,
            'a0': OrdinaryPlugin, 'f': FlatNavigationPlugin, 'b4': TreeNavigationPlugin}
TRACE_FIELDS = {"plugin", "keyword_count", "vector_count", "bm25_cache_hit", "budgets", "fused_count",
                "seeds", "extensions", "fill", "rejected", "extra_read_count", "embedding_calls", "embedding_usage",
                "fallback", "fallback_reason", "section_candidates", "selected_sections", "focused_children",
                "focused_keyword_count", "focused_vector_count", "focused_bm25_cache_hit", "navigation_error",
                "closures", "skipped_closures", "replacements", "structure_version_ids",
                "structural_additions", "a_retention_at_40", "structure_unavailable",
                "rerank_mode", "rerank_groups", "unit_hits", "unit_expansions", "skipped_units", "fallback_reason",
                "baseline_candidate_ids", "baseline_candidate_sources", "unit_decisions", "generation_source",
                'navigation', 'selected_node_ids', 'selected_packet_ids', 'selected_flat_unit_ids',
                'frontier_omitted_ids', 'skipped_packets', 'a_top20_retained'}
SAFE_ERRORS = {"VECTOR_INDEX_NOT_READY", "VECTOR_PROFILE_MISMATCH", "VECTOR_UNAVAILABLE", "VECTOR_INVALID_RESPONSE",
               "EMBEDDING_INPUT_INVALID", "EMBEDDING_UNAVAILABLE", "EMBEDDING_INVALID_RESPONSE", "RATE_LIMITED",
               "RETRIEVAL_DEADLINE_EXCEEDED", "RETRIEVAL_SCOPE_INVALID", "RETRIEVAL_INVALID_RESPONSE",
               "RAG_COST_BUDGET_EXCEEDED", "RAG_SPEND_CONFIG_INVALID", "RAG_DEADLINE_EXCEEDED"}


class ManagedPlugin:
    def __init__(self, name, implementation, *, source_resolver=None, runtime_validator=None,
                 source_validator=None, builder=None, delete_callback=None, close_callback=None):
        if name not in REGISTRY:
            raise ValueError("PLUGIN_UNKNOWN")
        self.name, self.implementation = name, implementation
        self.source_resolver, self.runtime_validator = source_resolver, runtime_validator
        self.source_validator, self.builder = source_validator, builder
        self.delete_callback, self.close_callback = delete_callback, close_callback
        self._closed = False
        self._traces = OrderedDict()
        self.last_trace = None

    def validate_runtime(self, manifests):
        if self._closed or self.runtime_validator is None:
            raise ValueError("PLUGIN_RUNTIME_VALIDATION_UNAVAILABLE")
        try:
            self.runtime_validator(deepcopy(manifests))
        except Exception:
            raise ValueError("PLUGIN_RUNTIME_INVALID") from None

    def for_sources(self, rows):
        """Return a request-local facade over already-authorized SQL source rows."""
        if self._closed:
            raise ValueError("PLUGIN_UNAVAILABLE")
        snapshot = deepcopy(list(rows))
        return ManagedPlugin(self.name, self.implementation, source_resolver=lambda request: deepcopy(snapshot),
            runtime_validator=self.runtime_validator, source_validator=self.source_validator,
            builder=self.builder, delete_callback=self.delete_callback)

    def prepare(self, snapshot, profile, task_context):
        if self._closed or self.builder is None:
            raise ValueError("PLUGIN_PREPARATION_UNAVAILABLE")
        try:
            if not isinstance(profile, dict) or set(profile) - {"max_tokens"}:
                raise ValueError("unsupported build profile")
            if not isinstance(task_context, dict) or set(task_context) - {"material_version_id", "retry"}:
                raise ValueError("unsupported build task context")
            max_tokens, retry = profile.get("max_tokens", 1800), task_context.get("retry", False)
            if type(max_tokens) is not int or max_tokens <= 0 or type(retry) is not bool:
                raise ValueError("invalid build parameters")
            self.builder._current(snapshot)
            manifest = self.builder.build(snapshot.space_id, task_context["material_version_id"],
                                          max_tokens=max_tokens, retry=retry)
            if self.name in {"b1", "b15", "b2_r1", "b3_unit"}:
                manifest = self.builder.build_tree(manifest, retry=retry)
            self.builder._current(snapshot)
            if (manifest["scope_snapshot_id"] != snapshot.scope_snapshot_id
                    or manifest["configuration"]["max_tokens"] != max_tokens):
                raise ValueError("prepared manifest does not match requested snapshot/profile")
            self.builder.validate(manifest)
            return deepcopy(manifest)
        except Exception:
            raise ValueError("PLUGIN_PREPARATION_FAILED") from None

    def _record(self, trace_id, trace):
        self.last_trace = dict(deepcopy(trace), trace_id=trace_id)
        self._traces[trace_id] = deepcopy(self.last_trace)
        while len(self._traces) > 64:
            self._traces.popitem(last=False)

    def get_trace(self, trace_id):
        return deepcopy(self._traces.get(trace_id))

    def retrieve(self, request):
        trace_id = uuid4().hex
        if self._closed or self.source_resolver is None or self.implementation is None:
            self._record(trace_id, {"error_code": "PLUGIN_UNAVAILABLE"})
            return RetrievalResult(status="unavailable", error_code="PLUGIN_UNAVAILABLE", trace_id=trace_id)
        try:
            if time.monotonic() >= request.deadline:
                raise ValueError("expired request")
            rows = list(self.source_resolver(request))
            by_id = {row["chunk_id"]: deepcopy(row) for row in rows}
            if len(by_id) != len(rows):
                raise ValueError("ambiguous authorized identities")
            if self.source_validator is not None:
                self.source_validator(request, rows)
            raw = self.implementation.retrieve(request, rows)
            candidates = tuple(Candidate.model_validate(value) for value in raw["candidates"])
            if (len(candidates) > request.budget.rerank_candidates
                    or len({c.chunk_id for c in candidates}) != len(candidates)):
                raise ValueError("invalid candidate count")
            for candidate in candidates:
                source = by_id.get(candidate.chunk_id)
                if source is None or (source["retrieval_version_id"], source["material_version_id"]) != (
                        candidate.retrieval_version_id, candidate.material_version_id):
                    raise ValueError("candidate outside authorized source identities")
            if time.monotonic() >= request.deadline:
                raise ValueError("expired request")
            trace = {key: value for key, value in raw.get("trace", {}).items() if key in TRACE_FIELDS}
            self._record(trace_id, trace)
            return RetrievalResult(status="ready", candidates=candidates, trace_id=trace_id)
        except Exception as error:
            code = error.code if isinstance(error, (RetrievalError, VerificationError)) and error.code in SAFE_ERRORS else "PLUGIN_RETRIEVAL_FAILED"
            self._record(trace_id, {"error_code": code})
            return RetrievalResult(status="failed", error_code=code, trace_id=trace_id)

    def delete(self, identity):
        try:
            if self._closed or self.delete_callback is None:
                raise ValueError("cleanup callback unavailable")
            if (not isinstance(identity, dict) or set(identity) - {"material_version_id", "retrieval_version_id", "collection"}
                    or any(not isinstance(identity.get(key), str) or not identity[key]
                           for key in ("material_version_id", "retrieval_version_id"))):
                raise ValueError("exact version identities required")
            receipt = self.delete_callback(deepcopy(identity))
            if not isinstance(receipt, dict) or receipt.get("verified") is not True:
                raise ValueError("unverified cleanup")
            return {"status": "deleted", "verified": True, "identity": deepcopy(identity)}
        except Exception:
            return {"status": "failed", "verified": False, "error_code": "PLUGIN_DELETE_FAILED"}

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.close_callback is not None:
            self.close_callback()
        elif hasattr(self.implementation, "close"):
            self.implementation.close()


def create_plugin(name, embedder, dense, *, bm25_profile=None, cache_size=8,
                  shared_cache=None, cache_lock=None, **adapter_options):
    try:
        plugin_type = REGISTRY[name]
    except KeyError:
        raise ValueError("PLUGIN_UNKNOWN") from None
    unit_dense = adapter_options.pop("unit_dense", None)
    navigation_index = adapter_options.pop('navigation_index', None)
    navigator = adapter_options.pop('navigator', None)
    implementation_kwargs = dict(bm25_profile=bm25_profile, cache_size=cache_size,
                                 shared_cache=shared_cache, cache_lock=cache_lock)
    if name == "b3_unit":
        implementation_kwargs["unit_dense"] = unit_dense
    if name in {'b4', 'f'}:
        implementation_kwargs.update(navigation_index=navigation_index, navigator=navigator)
    implementation = plugin_type(embedder, dense, **implementation_kwargs)
    def runtime_validator(manifests):
        for manifest in manifests:
            config = manifest["configuration"]
            if (config.get("vector_collection") != dense.collection
                    or config.get("embedding_model") != embedder.model_version
                    or config.get("embedding_dimension") != embedder.dimension
                    or config.get("bm25") != implementation.bm25_profile):
                raise ValueError("PLUGIN_PROFILE_MISMATCH")
    adapter_options.setdefault("runtime_validator", runtime_validator)
    adapter_options.setdefault("source_validator", lambda request, rows: dense.verify(rows))
    return ManagedPlugin(name, implementation, **adapter_options)
