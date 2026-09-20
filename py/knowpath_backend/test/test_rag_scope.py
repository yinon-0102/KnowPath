"""Scope is a union of original intervals, never a substring-match permission."""
import pytest

from knowpath_backend.learning.rag.contracts import SourceSpan, ScopeBinding
from knowpath_backend.learning.rag.scope import covers, freeze_scope, allowed_chunk_ids


def source(start, end, *, artifact="a", version="v1", page=1, block="p1"):
    return SourceSpan(material_version_id=version, artifact_hash=artifact * 64,
                      start=start, end=end, page=page, block=block)


def snapshot(spans, graph=1, scope_version=1):
    return freeze_scope(space_id="space", scope_version=scope_version,
        bindings=[ScopeBinding(material_id="m1", material_version_id="v1", graph_version=graph)],
        allowed_spans=spans)


def test_complete_union_coverage_allows_adjacent_spans_but_rejects_gap():
    assert covers(source(0, 20), [source(0, 10), source(10, 20)])
    assert not covers(source(0, 20), [source(0, 9), source(10, 20)])
    assert not covers(source(0, 21), [source(0, 20)])


@pytest.mark.parametrize("other", [source(0, 20, artifact="b"),
    source(0, 20, version="v2"), source(0, 20, page=2), source(0, 20, block="p2")])
def test_same_offsets_do_not_grant_access_across_coordinate_systems(other):
    assert not covers(source(0, 20), [other])


def test_mixed_allowed_and_excluded_chunk_is_rejected_as_a_whole():
    scope = snapshot([source(0, 10)])
    chunks = {"allowed": (source(0, 10),), "mixed": (source(0, 10), source(10, 20)),
              "empty": (), "overlap": (source(5, 15),)}
    assert allowed_chunk_ids(scope, chunks) == ("allowed",)


def test_scope_identity_changes_with_graph_or_visibility_but_not_rechunking():
    scope = snapshot([source(0, 10)])
    assert scope.scope_snapshot_id != snapshot([source(0, 10)], graph=2).scope_snapshot_id
    assert scope.scope_snapshot_id != snapshot([source(0, 10)], scope_version=2).scope_snapshot_id
    assert scope.scope_snapshot_id == snapshot([source(5, 10), source(0, 5)]).scope_snapshot_id
    assert allowed_chunk_ids(scope, {"new1": (source(0, 5),), "new2": (source(5, 10),)}) == ("new1", "new2")


def test_scope_cannot_grant_spans_of_unbound_version():
    with pytest.raises(ValueError, match="unbound"):
        snapshot([source(0, 10, version="other")])
