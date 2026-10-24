"""Keep legacy graph hash bytes stable across MySQL JSON number storage."""
import copy
import json
from hashlib import sha256

import pytest

from knowpath_backend.learning.knowledge.reconciliation import digest
from knowpath_backend.learning.workers.graph import preparation_manifest


def legacy_digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode()).hexdigest()


@pytest.fixture
def snapshot():
    return {"schema_version": 1, "material_id": "material_1",
            "material_version_id": "version_1",
            "nodes": [{"id": "topic_1", "name": "函数", "confidence": 1.0,
                       "level": 1, "alternatives": [{"confidence": 0.6}]}],
            "relations": [{"id": "relation_1", "confidence": 1.0}],
            "sources": [{"id": "chunk_1", "page": 1}]}


def revision(snapshot):
    return {"id": "revision_1", "snapshot": snapshot,
            "snapshot_hash": legacy_digest(snapshot), "base_graph_version": 1,
            "diff": {"conflicts": [{"before": {"confidence": 1.0, "level": 1}}]}}


def test_float_snapshot_and_manifest_keep_exact_legacy_hashes(snapshot):
    before = copy.deepcopy(snapshot)
    assert digest(snapshot) == legacy_digest(snapshot)
    manifest = preparation_manifest(revision(snapshot))
    payload = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    assert manifest["manifest_hash"] == legacy_digest(payload)
    assert snapshot == before


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_mysql_integer_confidence_keeps_snapshot_and_manifest_hashes(snapshot, confidence):
    snapshot["nodes"][0]["confidence"] = confidence
    snapshot["relations"][0]["confidence"] = confidence
    original = revision(snapshot)
    normalized = copy.deepcopy(original)
    normalized["snapshot"]["nodes"][0]["confidence"] = int(confidence)
    normalized["snapshot"]["relations"][0]["confidence"] = int(confidence)
    normalized["diff"]["conflicts"][0]["before"]["confidence"] = 1
    before = copy.deepcopy(normalized)
    assert digest(normalized["snapshot"]) == original["snapshot_hash"]
    assert preparation_manifest(normalized)["manifest_hash"] == preparation_manifest(original)["manifest_hash"]
    assert normalized == before
    assert type(normalized["snapshot"]["nodes"][0]["confidence"]) is int


@pytest.mark.parametrize("different", [0.0, 0.6, True, False, "1", "1.0", 2])
def test_confidence_changes_and_non_numeric_types_do_not_collide(different):
    assert digest({"confidence": different}) != digest({"confidence": 1.0})
    if type(different) is not int or different not in (0, 1):
        assert digest({"confidence": different}) == legacy_digest({"confidence": different})


@pytest.mark.parametrize("field", ["schema_version", "level", "page", "score"])
def test_unrelated_integer_fields_keep_distinct_legacy_hashes(field):
    integer = {"nested": [{field: 1, "confidence": 0.6}]}
    floating = {"nested": [{field: 1.0, "confidence": 0.6}]}
    assert digest(integer) == legacy_digest(integer)
    assert digest(floating) == legacy_digest(floating)
    assert digest(integer) != digest(floating)
