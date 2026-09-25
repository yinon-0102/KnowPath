"""An unchanged unit text/vector must not validate against a changed source map."""
from copy import deepcopy

import pytest
from qdrant_client import QdrantClient

from knowpath_backend.learning.rag.retrieval import RetrievalError
from knowpath_backend.learning.rag.unit_first import UnitFirstPlugin
from knowpath_backend.learning.rag.units import build_units
from knowpath_backend.learning.rag.vector import QdrantContentIndex
from knowpath_backend.test.test_rag_b3 import leaf


@pytest.fixture
def unit_index():
    client = QdrantClient(":memory:")
    index = QdrantContentIndex(client, "unit_binding", 2, "test-profile")
    rows = [leaf("anchor", "first", ordinal=0), leaf("child", "second", unit="anchor", ordinal=1)]
    unit = build_units(rows, tree_version_id=rows[0]["tree_version_id"])[0]
    row = UnitFirstPlugin._unit_row(unit)
    index.upsert([row], [[1.0, 0.0]])
    yield index, row
    client.close()


@pytest.mark.parametrize("field,value", [
    ("source_map_hash", "f" * 64), ("tree_version_id", "different-tree"),
    ("leaf_ids", ["anchor", "different-child"]),
    ("anchor_id", "different-anchor"),
])
def test_unit_payload_change_invalidates_existing_index(unit_index, field, value):
    index, original = unit_index
    changed = deepcopy(original)
    changed[field] = value
    with pytest.raises((RetrievalError, ValueError)):
        index.verify([changed])
    with pytest.raises((RetrievalError, ValueError)):
        index.search([1.0, 0.0], [changed])


def test_legacy_unit_payload_is_not_accepted_as_bound(unit_index):
    index, row = unit_index
    legacy = {k: v for k, v in row.items() if k not in {"source_map_hash", "leaf_ids", "anchor_id"}}
    index.upsert([legacy], [[1.0, 0.0]])
    with pytest.raises(RetrievalError, match="VECTOR_INDEX_NOT_READY"):
        index.verify([row])


def test_bound_unit_remains_searchable_and_regular_leaf_payload_stays_compatible(unit_index):
    index, row = unit_index
    assert index.verify([row])["verified"]
    assert index.search([1.0, 0.0], [row])[0][0] == row["chunk_id"]
    regular = leaf("regular", "text", ordinal=2)
    assert "semantic_unit" not in index._payload(regular)
