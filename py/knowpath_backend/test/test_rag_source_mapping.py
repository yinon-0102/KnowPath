"""New retrieval spans bridge to real legacy sources without changing old refs."""
from copy import deepcopy
from hashlib import sha256

import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.rag.sources import CitationResolver
from knowpath_backend.learning.materials.source_access import SourceAccessService
from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions


@pytest.fixture(params=["memory", "sql"])
def workspace(request, tmp_path):
    engine = None
    if request.param == "sql":
        engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'mapping.db').as_posix()}")
        init_db(engine)
    state = LearningState(SqlAlchemyMaterialRepository(engine) if engine is not None else None,
                          question_generator=FixedQuestions())
    uploaded = state.material_service.create(filename="scope.md",
        content="# Allowed\n\nConditions apply.\n\n# Excluded\n\nSecret exception.".encode(),
        idempotency_key="source")
    space = state.create_space({"name": "Study", "material_ids": [uploaded.material.id]})
    yield state, uploaded, space
    if engine is not None:
        engine.dispose()


def ref(uploaded, index=0):
    return dict(material_id=uploaded.material.id, material_version_id=uploaded.version.id,
                chunk_id=uploaded.version.chunks[index].id)


def span(uploaded, index=0):
    chunk = uploaded.version.chunks[index]
    return dict(material_version_id=uploaded.version.id, artifact_hash=sha256(chunk.text.encode()).hexdigest(),
                start=0, end=len(chunk.text), page=chunk.page, block=chunk.id)


class ChunkStore:
    def __init__(self, uploaded):
        self.chunk = dict(retrieval_version_id="r1", chunk_id="new", material_version_id=uploaded.version.id,
                          source_text=uploaded.version.chunks[0].text, source_spans=[span(uploaded)])

    def get_chunk(self, retrieval_version_id, chunk_id):
        return deepcopy(self.chunk) if (retrieval_version_id, chunk_id) == ("r1", "new") else None


def test_space_snapshot_keeps_graph_identity_and_excludes_aliases(workspace):
    state, uploaded, space = workspace
    before = state.space_service.rag_scope_snapshot(space["id"])
    state.space_service.set_scope(space["id"], dict(topic_ids=["topic_allowed"],
        excluded_topic_ids=["topic_excluded"], expected_version=1))
    after = state.space_service.rag_scope_snapshot(space["id"])
    assert before.scope_snapshot_id != after.scope_snapshot_id
    assert len(before.allowed_spans) == 2 and len(after.allowed_spans) == 1
    assert after.bindings[0].graph_version == 1
    assert after.allowed_spans[0].block == uploaded.version.chunks[0].id


def test_legacy_reference_keeps_location_and_cannot_read_excluded_chunk(workspace):
    state, uploaded, space = workspace
    resolver = CitationResolver(state.material_service.repository, state.space_service,
                                source_access=SourceAccessService(state.assessment_service))
    original = ref(uploaded)
    saved = deepcopy(original)
    result = resolver.resolve(original, space_id=space["id"])
    assert result["text"] == uploaded.version.chunks[0].text
    assert result["line_start"] == uploaded.version.chunks[0].line_start
    assert original == saved
    state.space_service.set_scope(space["id"], dict(topic_ids=["topic_allowed"],
        excluded_topic_ids=["topic_excluded"], expected_version=1))
    with pytest.raises(DomainConflict, match="范围"):
        resolver.resolve(ref(uploaded, 1), space_id=space["id"])


def test_new_citation_uses_authoritative_original_and_records_legacy_consultation(workspace):
    state, uploaded, space = workspace
    recorded = []
    class Audit:
        def consult(self, material_id, version_id, chunk_ids, space_id):
            recorded.append((material_id, version_id, chunk_ids, space_id))
    store = ChunkStore(uploaded)
    # Plugin/index body must never replace original evidence.
    store.chunk["source_text"] = "tampered candidate text"
    resolver = CitationResolver(state.material_service.repository, state.space_service,
                                rag_repository=store, source_access=Audit())
    citation = {**ref(uploaded), "citation_schema_version": 2, "retrieval_version_id": "r1",
                "chunk_id": "new", "source_spans": [span(uploaded)]}
    result = resolver.resolve(citation, space_id=space["id"])
    assert result["text"] == uploaded.version.chunks[0].text
    assert recorded[0][2] == [uploaded.version.chunks[0].id]


@pytest.mark.parametrize("change", ["excluded", "hash", "version", "deleted", "unknown_schema"])
def test_new_reference_fails_closed_on_unverifiable_or_revoked_source(workspace, change):
    state, uploaded, space = workspace
    store = ChunkStore(uploaded)
    citation = {**ref(uploaded), "citation_schema_version": 2, "retrieval_version_id": "r1",
                "chunk_id": "new", "source_spans": [span(uploaded)]}
    if change == "excluded":
        store.chunk["source_spans"].append(span(uploaded, 1))
        state.space_service.set_scope(space["id"], dict(topic_ids=["topic_allowed"],
            excluded_topic_ids=["topic_excluded"], expected_version=1))
    elif change == "hash":
        citation["source_spans"][0]["artifact_hash"] = "b" * 64
    elif change == "version":
        store.chunk["material_version_id"] = "other"
    elif change == "deleted":
        state.material_service.repository.delete_material(uploaded.material.id)
    else:
        citation["citation_schema_version"] = 99
    resolver = CitationResolver(state.material_service.repository, state.space_service, rag_repository=store,
                                source_access=SourceAccessService(state.assessment_service))
    with pytest.raises((DomainConflict, ValueError)):
        resolver.resolve(citation, space_id=space["id"])


@pytest.mark.parametrize("pending", [False, True])
def test_v2_source_read_marks_existing_and_pending_assessment_as_assisted(workspace, pending):
    state, uploaded, space = workspace
    queued = []
    assessment = state.create_assessment(space["id"], dict(question_count=5,
        difficulty_mix={"easy": 1.0, "medium": 0.0, "hard": 0.0}),
        dispatch=(lambda fn, *args: queued.append((fn, args))) if pending else None)
    access = SourceAccessService(state.assessment_service)
    citation = {**ref(uploaded), "citation_schema_version": 2, "retrieval_version_id": "r1",
                "chunk_id": "new", "source_spans": [span(uploaded)]}
    access.consult_citation(citation, space["id"], rag_repository=ChunkStore(uploaded))
    if pending:
        fn, args = queued[0]
        fn(*args)
    stored = state.assessment_service.repository.get_record("assessments", assessment["id"])
    assert stored["status"] == "ready"
    consulted = stored["snapshot"]["consulted_sources"]
    assert consulted[0]["chunk_id"] == uploaded.version.chunks[0].id
    affected = [q for q in stored["questions"] if any(r["chunk_id"] == consulted[0]["chunk_id"]
                                                    for r in q["source_refs"])]
    assert affected and all(q.get("assisted") for q in affected)


def test_revocation_during_audit_prevents_text_return(workspace):
    state, uploaded, space = workspace
    class Revoke:
        def consult(self, *args):
            state.space_service.set_scope(space["id"], dict(topic_ids=["topic_excluded"],
                excluded_topic_ids=["topic_allowed"], expected_version=1))
    resolver = CitationResolver(state.material_service.repository, state.space_service, source_access=Revoke())
    with pytest.raises(DomainConflict) as error:
        resolver.resolve(ref(uploaded), space_id=space["id"])
    assert error.value.code == "SCOPE_CHANGED"


def test_sql_snapshot_and_rechunked_citation_roundtrip(workspace):
    state, uploaded, space = workspace
    uow = getattr(state.material_service.repository, "unit_of_work", None)
    if uow is None:
        pytest.skip("SQL persistence integration variant")
    from knowpath_backend.learning.persistence.rag_repository import SqlRagRepository
    store = SqlRagRepository(uow.engine)
    frozen = state.space_service.rag_scope_snapshot(space["id"])
    store.put_scope_snapshot(frozen.model_dump(exclude={"allowed_spans"}),
                             [s.model_dump() for s in frozen.allowed_spans])
    store.put_retrieval_version(dict(retrieval_version_id="r1", material_version_id=uploaded.version.id,
        profile={"chunking": "legacy-bridge-v1"}, profile_hash="b" * 64))
    store.put_chunk(dict(retrieval_version_id="r1", chunk_id="new", material_version_id=uploaded.version.id,
        source_text=uploaded.version.chunks[0].text), [span(uploaded)])
    store.put_scope_chunk_map(dict(scope_snapshot_id=frozen.scope_snapshot_id,
        retrieval_version_id="r1", chunk_id="new"))
    assert [c["chunk_id"] for c in store.list_scope_chunks(frozen.scope_snapshot_id, "r1")] == ["new"]
    citation = {**ref(uploaded), "citation_schema_version": 2, "retrieval_version_id": "r1",
                "chunk_id": "new", "source_spans": [span(uploaded)]}
    resolved = SourceAccessService(state.assessment_service).consult_citation(citation, space["id"], rag_repository=store)
    assert resolved["text"] == uploaded.version.chunks[0].text
    assert state.material_service.repository.get_version(uploaded.version.id).chunks == uploaded.version.chunks
