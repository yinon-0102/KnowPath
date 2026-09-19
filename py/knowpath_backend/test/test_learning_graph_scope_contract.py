"""Grounded graph extraction and pinned prerequisite scope contract."""
from copy import deepcopy

import pytest
from knowpath_backend.learning.knowledge.reconciliation import extract_snapshot
from knowpath_backend.learning.state import LearningState


def workspace(text):
    state = LearningState()
    upload = state.material_service.create(filename="relations.md", content=text.encode(), idempotency_key="upload")
    return state, upload


def publish_fixture(state, upload, snapshot):
    # This fixture isolates scope against an immutable published snapshot; graph
    # publication itself is covered separately through the real worker.
    record = {"id": "revision", "material_id": upload.material.id,
              "material_version_id": upload.version.id, "graph_version": 1,
              "sequence": 1, "status": "published", "publication": {"snapshot": snapshot}}
    state.graph_service.repository.put_record("graph_revisions", record)
    return state.space_service.create({"name": "Space", "material_ids": [upload.material.id]}, "space")


def edge(snapshot, source, target, *, status="active", confirmed=True, sourced=True):
    by_name = {node["name"]: node for node in snapshot["nodes"]}
    return {"id": source + target, "type": "prerequisite_of", "from_id": by_name[source]["id"],
            "to_id": by_name[target]["id"], "status": status, "confirmed": confirmed,
            "source_refs": deepcopy(by_name[target]["source_refs"]) if sourced else [], "confidence": 1.0}


def test_heading_hierarchy_is_sourced_without_inventing_sequential_dependencies():
    state, upload = workspace("# Language\n\n## Basics\n\nNames bind values.\n\n## Functions\n\nReusable operations.")
    snapshot = extract_snapshot(upload.material.id, upload.version)
    nodes = {n["name"]: n for n in snapshot["nodes"]}
    assert set(nodes) == {"Language", "Basics", "Functions"}
    assert nodes["Basics"]["parent_id"] == nodes["Language"]["id"]
    assert nodes["Functions"]["parent_id"] == nodes["Language"]["id"]
    assert len(snapshot["relations"]) == 2
    assert all(r["type"] == "contains" and r["source_refs"] for r in snapshot["relations"])


def test_explicit_prerequisites_and_related_topics_are_grounded_candidates():
    state, upload = workspace("# Basics\n\nNames bind values.\n\n# Functions\n\nPrerequisites: Basics\nRelated topics: Basics")
    snapshot = extract_snapshot(upload.material.id, upload.version)
    assert {r["type"] for r in snapshot["relations"]} == {"prerequisite_of", "related_to"}
    prerequisite = next(r for r in snapshot["relations"] if r["type"] == "prerequisite_of")
    assert prerequisite["status"] == "pending"
    assert prerequisite["confirmed"] is False
    assert prerequisite["source_refs"]
    assert prerequisite["evidence"] == "Prerequisites: Basics"


def test_scope_expands_transitive_confirmed_prerequisites_from_pinned_snapshot():
    state, upload = workspace("# A\n\nA fact.\n\n# B\n\nB fact.\n\n# C\n\nC fact.")
    snapshot = extract_snapshot(upload.material.id, upload.version)
    snapshot["relations"] = [edge(snapshot, "A", "B"), edge(snapshot, "B", "C")]
    space = publish_fixture(state, upload, snapshot)
    ids = {n["name"]: n["id"] for n in snapshot["nodes"]}
    result = state.space_service.set_scope(space["id"], {"topic_ids": [ids["C"]], "expected_version": 1})
    assert set(result["topic_ids"]) == set(ids.values())
    assert set(result["prerequisite_topic_ids"]) == {ids["A"], ids["B"]}
    assert result["recommended_topic_ids"] == []
    topics = {n["id"]: n for n in state.space_service.bound_topics(state.space_service.get(space["id"]))}
    assert topics[ids["C"]]["prerequisites"] == [ids["B"]]
    assert topics["topic_c"]["prerequisites"] == ["topic_b"]


@pytest.mark.parametrize("kind", ["pending", "unconfirmed", "unsourced", "excluded", "cycle", "disabled"])
def test_scope_never_silently_adds_unusable_prerequisites(kind):
    state, upload = workspace("# A\n\nA fact.\n\n# B\n\nB fact.")
    snapshot = extract_snapshot(upload.material.id, upload.version)
    relation = edge(snapshot, "A", "B", status="pending" if kind == "pending" else "active",
                    confirmed=kind != "unconfirmed", sourced=kind != "unsourced")
    snapshot["relations"] = [relation]
    if kind == "cycle":
        snapshot["relations"].append(edge(snapshot, "B", "A"))
    space = publish_fixture(state, upload, snapshot)
    ids = {n["name"]: n["id"] for n in snapshot["nodes"]}
    payload = {"topic_ids": [ids["B"]], "expected_version": 1, "include_prerequisites": kind != "disabled"}
    if kind == "excluded":
        payload["excluded_topic_ids"] = [ids["A"]]
    result = state.space_service.set_scope(space["id"], payload)
    assert result["topic_ids"] == [ids["B"]]
    assert result["prerequisite_topic_ids"] == []
    assert result["recommended_topic_ids"] == ([] if kind == "excluded" else [ids["A"]])


def test_reviewed_publication_activates_only_grounded_acyclic_prerequisites():
    from knowpath_backend.test.test_learning_graph_worker import worker, event_id
    state, upload = workspace("# A\n\nBase fact.\n\n# B\n\nPrerequisites: A")
    staged = state.graph_service.reconcile(upload.material.id,
        {"version_id": upload.version.id, "expected_graph_version": 0}, "reconcile")
    worker(state).run_once(event_id(state, staged))
    state.graph_service.publish(upload.material.id, staged["candidate_revision_id"],
        {"expected_graph_version": 0, "resolutions": []}, "publish")
    revision = state.graph_service.repository.get_record("graph_revisions", staged["candidate_revision_id"])
    assert revision["snapshot"]["relations"][0]["status"] == "pending"
    published = revision["publication"]["snapshot"]
    assert published["relations"][0]["status"] == "active"
    assert published["relations"][0]["confirmed"] is True
    space = state.space_service.create({"material_ids": [upload.material.id]}, "space")
    ids = {n["name"]: n["id"] for n in published["nodes"]}
    result = state.space_service.set_scope(space["id"], {"topic_ids": [ids["B"]], "expected_version": 1})
    assert result["prerequisite_topic_ids"] == [ids["A"]]


def test_publication_rejects_cycles_without_mutating_candidate_or_published_pointer():
    from knowpath_backend.learning.errors import DomainConflict
    from knowpath_backend.test.test_learning_graph_worker import worker, event_id
    state, upload = workspace("# A\n\nPrerequisites: B\n\n# B\n\nPrerequisites: A")
    staged = state.graph_service.reconcile(upload.material.id,
        {"version_id": upload.version.id, "expected_graph_version": 0}, "reconcile")
    worker(state).run_once(event_id(state, staged))
    before = state.graph_service.repository.get_record("graph_revisions", staged["candidate_revision_id"])
    with pytest.raises(DomainConflict) as failure:
        state.graph_service.publish(upload.material.id, staged["candidate_revision_id"],
            {"expected_graph_version": 0, "resolutions": []}, "publish")
    assert failure.value.code == "PREREQUISITE_CYCLE"
    after = state.graph_service.repository.get_record("graph_revisions", staged["candidate_revision_id"])
    assert after == before
    assert all(edge["status"] == "pending" for edge in after["snapshot"]["relations"])
    assert state.graph_service._published(state.graph_service._history(upload.material.id)) is None


def test_relation_changes_identify_both_affected_topics():
    from knowpath_backend.learning.knowledge.reconciliation import compare_snapshots
    state, upload = workspace("# A\n\nBase.\n\n# B\n\nDependent.")
    old = extract_snapshot(upload.material.id, upload.version)
    new = deepcopy(old)
    new["relations"] = [edge(new, "A", "B")]
    assert set(compare_snapshots(old, new)["affected_topic_ids"]) == {node["id"] for node in new["nodes"]}


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_scope_invalidates_persisted_plan_atomically(storage, tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from knowpath_backend.learning.persistence.db import init_db
    from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
    from knowpath_backend.learning.spaces.service import now
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'scope.db'}") if storage == "sqlite" else None
    if engine:
        init_db(engine)
    state = LearningState(SqlAlchemyMaterialRepository(engine)) if engine else LearningState()
    upload = state.material_service.create(filename="notes.md", content=b"# A\n\nSome fact.", idempotency_key="upload")
    space = state.space_service.create({"material_ids": [upload.material.id]}, "space")
    topic = state.space_service.bound_topics(space)[0]["id"]
    repository = state.graph_service.repository
    repository.put_record("plans", {"id": "plan", "space_id": space["id"], "version": 1, "status": "active",
                                   "scope_version": 0, "config": {}, "run_id": None, "created_at": now()})
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise RuntimeError("replay failure")
        patch.setattr(state.space_service.repository, "remember", fail)
        with pytest.raises(RuntimeError, match="replay failure"):
            state.space_service.set_scope(space["id"], {"topic_ids": [topic], "expected_version": 1}, "scope")
    assert repository.get_record("plans", "plan")["status"] == "active"
    state.space_service.set_scope(space["id"], {"topic_ids": [topic], "expected_version": 1}, "scope")
    assert repository.get_record("plans", "plan")["status"] == "needs_replan"
    if engine:
        engine.dispose()


@pytest.mark.parametrize("line", ["Basics is a prerequisite of Functions.", "Functions requires Basics.", "Basics是Functions的前置知识。", "学习Functions需要先掌握Basics。"])
def test_explicit_source_sentences_create_only_grounded_prerequisite_candidates(line):
    state, upload = workspace("# Basics\n\nBase fact.\n\n# Functions\n\n" + line)
    snapshot = extract_snapshot(upload.material.id, upload.version)
    relation = next(r for r in snapshot["relations"] if r["type"] == "prerequisite_of")
    ids = {node["name"]: node["id"] for node in snapshot["nodes"]}
    assert (relation["from_id"], relation["to_id"]) == (ids["Basics"], ids["Functions"])
    assert relation["evidence"] == line
    assert relation["status"] == "pending"


def test_reference_to_wrong_material_version_is_never_automatically_added():
    state, upload = workspace("# A\n\nBase fact.\n\n# B\n\nDependent fact.")
    snapshot = extract_snapshot(upload.material.id, upload.version)
    relation = edge(snapshot, "A", "B")
    relation["source_refs"][0]["material_version_id"] = "unrelated-version"
    snapshot["relations"] = [relation]
    space = publish_fixture(state, upload, snapshot)
    ids = {node["name"]: node["id"] for node in snapshot["nodes"]}
    result = state.space_service.set_scope(space["id"], {"topic_ids": [ids["B"]], "expected_version": 1})
    assert result["topic_ids"] == [ids["B"]]
    assert result["recommended_topic_ids"] == [ids["A"]]


def test_exclusion_uses_canonical_identity_and_fixed_published_binding():
    from knowpath_backend.learning.errors import DomainConflict
    state, upload = workspace("# A\n\nBase fact.\n\n# B\n\nDependent fact.")
    snapshot = extract_snapshot(upload.material.id, upload.version)
    snapshot["relations"] = [edge(snapshot, "A", "B")]
    space = publish_fixture(state, upload, snapshot)
    # A newer publication must not affect an existing space until explicit adoption.
    newer = deepcopy(state.graph_service.repository.get_record("graph_revisions", "revision"))
    newer.update(id="newer", graph_version=2, sequence=2)
    newer["publication"]["snapshot"]["relations"] = []
    state.graph_service.repository.put_record("graph_revisions", newer)
    ids = {node["name"]: node["id"] for node in snapshot["nodes"]}
    with pytest.raises(DomainConflict, match="别名"):
        state.space_service.set_scope(space["id"], {"topic_ids": ["topic_a"], "excluded_topic_ids": [ids["A"]], "expected_version": 1})
    result = state.space_service.set_scope(space["id"], {"topic_ids": ["topic_b"], "expected_version": 1})
    assert result["prerequisite_topic_ids"] == ["topic_a"]


@pytest.mark.parametrize("declaration,kind", [("Related topics: A", "related_to"), ("Prerequisites: A", "prerequisite_of")])
def test_keep_old_retains_relation_only_source_without_activating_new_claim(declaration, kind):
    from knowpath_backend.test.test_learning_graph_worker import worker, event_id
    state, upload = workspace("# A\n\nBase fact.\n\n# B\n\nOld B definition.")
    first = state.graph_service.reconcile(upload.material.id,
        {"version_id": upload.version.id, "expected_graph_version": 0}, "first-reconcile")
    worker(state).run_once(event_id(state, first))
    state.graph_service.publish(upload.material.id, first["candidate_revision_id"],
        {"expected_graph_version": 0, "resolutions": []}, "first-publish")
    newer = state.material_service.create_version(material_id=upload.material.id, filename="new.md",
        content=("# A\n\nBase fact.\n\n# B\n\nNew B definition. " + "\n" + declaration).encode(), idempotency_key="version")
    staged = state.graph_service.reconcile(upload.material.id,
        {"version_id": newer.version.id, "expected_graph_version": 1}, "reconcile")
    worker(state).run_once(event_id(state, staged))
    candidate = state.graph_service.repository.get_record("graph_revisions", staged["candidate_revision_id"])
    resolutions = [{"conflict_id": row["conflict_id"], "action": "keep_old", "reason": "Retain reviewed definition"}
                   for row in candidate["diff"]["conflicts"]]
    state.graph_service.publish(upload.material.id, staged["candidate_revision_id"],
        {"expected_graph_version": 1, "resolutions": resolutions}, "publish")
    publication = state.graph_service.repository.get_record("graph_revisions", staged["candidate_revision_id"])["publication"]["snapshot"]
    relation = next(edge for edge in publication["relations"] if edge["type"] == kind)
    sources = {source["id"]: source for source in publication["sources"]}
    node_refs = {ref["chunk_id"] for node in publication["nodes"] for ref in node["source_refs"]}
    assert any(ref["chunk_id"] not in node_refs for ref in relation["source_refs"])
    assert all(ref["chunk_id"] in sources for ref in relation["source_refs"])
    assert relation["status"] == "pending"
    assert relation["confirmed"] is False


@pytest.mark.parametrize("kind", ["contains", "related_to", "prerequisite_of"])
@pytest.mark.parametrize("failure", ["missing", "wrong_version"])
def test_all_relation_kinds_reject_dangling_or_foreign_source_references(kind, failure):
    from knowpath_backend.learning.errors import DomainConflict
    from knowpath_backend.learning.knowledge.relations import confirm_grounded_relations
    state, upload = workspace("# A\n\nBase fact.\n\n# B\n\nDependent fact.")
    snapshot = extract_snapshot(upload.material.id, upload.version)
    relation = edge(snapshot, "A", "B")
    relation.update(type=kind, status="pending", confirmed=False, extraction_method="explicit_source_declaration")
    relation["source_refs"][0]["chunk_id" if failure == "missing" else "material_version_id"] = "foreign"
    snapshot["relations"] = [relation]
    with pytest.raises(DomainConflict) as error:
        confirm_grounded_relations(snapshot)
    assert error.value.code == "INVALID_RELATION_SOURCE"
