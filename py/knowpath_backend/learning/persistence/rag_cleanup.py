"""Erase SQL RAG derivatives inside the existing owner's deletion transaction."""
import sqlalchemy as sa

from .db import Base
from .rag_models import TABLES


def _purge_scopes(session, scope_ids):
    if not scope_ids:
        return
    for name in ("rag_publications", "rag_manifests", "rag_scope_chunk_map",
                 "rag_scope_spans", "rag_scope_snapshots"):
        table = TABLES[name]
        session.execute(table.delete().where(table.c.scope_snapshot_id.in_(scope_ids)))


def erase_space_rag(session, space_id):
    table = TABLES["rag_scope_snapshots"]
    scopes = list(session.execute(sa.select(table.c.scope_snapshot_id).where(
        table.c.space_id == space_id).with_for_update()).scalars())
    _purge_scopes(session, scopes)


def erase_material_rag(session, material_id):
    originals = Base.metadata.tables["material_versions"]
    versions = set(session.execute(sa.select(originals.c.id).where(
        originals.c.material_id == material_id).with_for_update()).scalars())
    if not versions:
        return
    scopes = TABLES["rag_scope_snapshots"]
    # Empty scopes still bind original versions; invalidate them as well.
    scope_ids = [row.scope_snapshot_id for row in session.execute(sa.select(
        scopes.c.scope_snapshot_id, scopes.c.bindings).order_by(scopes.c.scope_snapshot_id).with_for_update()).all()
        if any(binding["material_version_id"] in versions for binding in row.bindings)]
    _purge_scopes(session, scope_ids)
    retrievals = TABLES["rag_retrieval_versions"]
    identifiers = list(session.execute(sa.select(retrievals.c.retrieval_version_id).where(
        retrievals.c.material_version_id.in_(versions)).with_for_update()).scalars())
    if not identifiers:
        return
    for name in ("rag_publications", "rag_manifests", "rag_scope_chunk_map", "rag_chunk_spans"):
        table = TABLES[name]
        session.execute(table.delete().where(table.c.retrieval_version_id.in_(identifiers)))
    nodes = TABLES["rag_nodes"]
    # Break self-FKs only during erasure, independent of row visitation order.
    session.execute(nodes.update().where(nodes.c.retrieval_version_id.in_(identifiers)).values(parent_node_id=None))
    session.execute(nodes.delete().where(nodes.c.retrieval_version_id.in_(identifiers)))
    chunks = TABLES["rag_chunks"]
    session.execute(chunks.delete().where(chunks.c.retrieval_version_id.in_(identifiers)))
    session.execute(retrievals.delete().where(retrievals.c.retrieval_version_id.in_(identifiers)))
