"""Managed plugins expose contract results, not source text or provider failures."""
from types import SimpleNamespace
import time

import pytest

from knowpath_backend.learning.rag.contracts import RetrievalBudget, RetrievalRequest, ScopeSnapshot


def request():
    return RetrievalRequest(query="question", original_query="question", scope_snapshot_id="scope",
        manifest_ids=("manifest",), budget=RetrievalBudget(), deadline=time.monotonic() + 60)


def rows():
    return [dict(chunk_id="chunk", retrieval_version_id="rv", material_version_id="mv", source_text="private source")]


def candidate(**extra):
    return dict(chunk_id="chunk", retrieval_version_id="rv", material_version_id="mv", channel="hybrid", rank=1, **extra)


def test_managed_retrieval_returns_contract_candidates_and_trace():
    from knowpath_backend.learning.rag.registry import ManagedPlugin
    adapter = ManagedPlugin("a", SimpleNamespace(retrieve=lambda req, source: {
        "candidates": [candidate()], "trace": {"keyword_count": 1}}), source_resolver=lambda req: rows(),
        source_validator=lambda req, source: None)
    result = adapter.retrieve(request())
    assert result.status == "ready" and result.candidates[0].chunk_id == "chunk"
    assert "private source" not in result.model_dump_json()
    assert adapter.get_trace(result.trace_id)["keyword_count"] == 1
    assert adapter.last_trace["trace_id"] == result.trace_id


def test_adapter_rejects_outside_scope_and_candidate_text_without_leaking_exception():
    from knowpath_backend.learning.rag.registry import ManagedPlugin
    for selected in (dict(candidate(), chunk_id="outside"), candidate(text="untrusted source")):
        adapter = ManagedPlugin("a", SimpleNamespace(retrieve=lambda req, source: {
            "candidates": [selected], "trace": {}}), source_resolver=lambda req: rows())
        result = adapter.retrieve(request())
        assert result.status == "failed" and not result.candidates and result.error_code
    def unavailable(*args): raise RuntimeError("provider secret")
    adapter = ManagedPlugin("a", SimpleNamespace(retrieve=unavailable), source_resolver=lambda req: rows())
    assert "secret" not in adapter.retrieve(request()).model_dump_json()


def test_registry_modes_runtime_validation_and_close_are_explicit():
    from knowpath_backend.learning.rag.registry import ManagedPlugin, create_plugin
    for name in ("a", "b1"):
        plugin = create_plugin(name, SimpleNamespace(model_version="model", dimension=4),
            SimpleNamespace(collection="collection", verify=lambda r: {"verified": True}))
        assert isinstance(plugin, ManagedPlugin)
        plugin.validate_runtime([dict(configuration={"embedding_model": "model", "embedding_dimension": 4,
            "vector_collection": "collection", "bm25": plugin.implementation.bm25_profile})])
        with pytest.raises(ValueError):
            plugin.validate_runtime([dict(configuration={})])
    with pytest.raises(ValueError): create_plugin("unknown", None, None)
    closed = []
    plugin = ManagedPlugin("a", None, close_callback=lambda: closed.append(True))
    plugin.close()
    plugin.close()
    assert closed == [True]
    assert plugin.retrieve(request()).status == "unavailable"


def test_prepare_delegates_builder_and_checks_exact_snapshot_and_profile():
    from knowpath_backend.learning.rag.registry import ManagedPlugin
    snapshot = ScopeSnapshot(scope_snapshot_id="scope", space_id="space", scope_version=1, bindings=(), allowed_spans=())
    calls = []
    manifest = dict(scope_snapshot_id="scope", configuration={"max_tokens": 500}, status="ready", b1_ready=True)
    builder = SimpleNamespace(_current=lambda scope: calls.append("scope"),
        build=lambda *args, **kwargs: dict(manifest), build_tree=lambda ordinary, **kwargs: dict(ordinary),
        validate=lambda value: calls.append("validate"))
    plugin = ManagedPlugin("b1", None, builder=builder)
    assert plugin.prepare(snapshot, {"max_tokens": 500}, {"material_version_id": "mv"}) == manifest
    assert calls == ["scope", "scope", "validate"]
    manifest["scope_snapshot_id"] = "changed"
    with pytest.raises(ValueError): plugin.prepare(snapshot, {"max_tokens": 500}, {"material_version_id": "mv"})


def test_delete_uses_exact_identity_callback_and_failure_is_explicit():
    from knowpath_backend.learning.rag.registry import ManagedPlugin
    captured = []
    plugin = ManagedPlugin("a", None, delete_callback=lambda identity: captured.append(identity) or {"verified": True})
    identity = dict(material_version_id="mv", retrieval_version_id="rv")
    assert plugin.delete(identity)["status"] == "deleted"
    assert captured == [identity]
    assert plugin.delete({})["status"] == "failed"


def test_closed_adapter_cannot_reopen_with_sources():
    from knowpath_backend.learning.rag.registry import ManagedPlugin
    adapter = ManagedPlugin("a", None)
    adapter.close()
    with pytest.raises(ValueError): adapter.for_sources(rows())


def test_implementation_cannot_mutate_authorized_version_identity():
    from knowpath_backend.learning.rag.registry import ManagedPlugin
    def mutate(req, sources):
        sources[0]["material_version_id"] = "outside"
        return {"candidates": [dict(candidate(), material_version_id="outside")], "trace": {}}
    adapter = ManagedPlugin("a", SimpleNamespace(retrieve=mutate), source_resolver=lambda req: rows())
    assert adapter.retrieve(request()).status == "failed"


def test_failing_runtime_validator_never_exposes_provider_details():
    from knowpath_backend.learning.rag.registry import ManagedPlugin
    def fail(manifests): raise RuntimeError("provider secret")
    adapter = ManagedPlugin("a", None, runtime_validator=fail)
    with pytest.raises(ValueError, match="PLUGIN_RUNTIME_INVALID") as error:
        adapter.validate_runtime([])
    assert "secret" not in str(error.value)


def test_facade_keeps_safe_embedding_usage_and_known_service_error_codes():
    from knowpath_backend.learning.rag.registry import ManagedPlugin
    from knowpath_backend.learning.rag.retrieval import RetrievalError
    adapter = ManagedPlugin("a", SimpleNamespace(retrieve=lambda req, source: {"candidates": [candidate()],
        "trace": {"embedding_calls": 1, "embedding_usage": {"total_tokens": 5, "model": "test"}}}),
        source_resolver=lambda req: rows())
    assert adapter.retrieve(request()).status == "ready"
    assert adapter.last_trace["embedding_calls"] == 1
    assert adapter.last_trace["embedding_usage"] == {"total_tokens": 5, "model": "test"}
    def invalid(req, source): raise RetrievalError("VECTOR_INDEX_NOT_READY")
    adapter.source_validator = invalid
    assert adapter.retrieve(request()).error_code == "VECTOR_INDEX_NOT_READY"
    def secret(req, source): raise RetrievalError("provider secret")
    adapter.source_validator = secret
    assert adapter.retrieve(request()).error_code == "PLUGIN_RETRIEVAL_FAILED"
