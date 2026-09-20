"""Additive SQLAlchemy RAG schema, registered by persistence.db."""

import sqlalchemy as sa

from knowpath_backend.learning.persistence.db import Base


def _tables(metadata):
    """Declare only the nine RAG tables; old source references are untouched."""
    tables = []
    def table(name, *columns):
        result = sa.Table(name, metadata, *columns)
        tables.append(result)
        return result

    table("rag_retrieval_versions",
        sa.Column("retrieval_version_id", sa.String(128), primary_key=True),
        sa.Column("material_version_id", sa.String(36), sa.ForeignKey("material_versions.id"), nullable=False),
        sa.UniqueConstraint("retrieval_version_id", "material_version_id", name="uq_rag_retrieval_material"),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("profile", sa.JSON, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False))
    table("rag_chunks",
        sa.Column("retrieval_version_id", sa.String(128), sa.ForeignKey("rag_retrieval_versions.retrieval_version_id"), primary_key=True),
        sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.Column("material_version_id", sa.String(36), sa.ForeignKey("material_versions.id"), nullable=False),
        sa.Column("source_text", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.ForeignKeyConstraint(["retrieval_version_id", "material_version_id"], ["rag_retrieval_versions.retrieval_version_id", "rag_retrieval_versions.material_version_id"], name="fk_rag_chunk_retrieval_material"),
        sa.UniqueConstraint("retrieval_version_id", "chunk_id", "material_version_id", name="uq_rag_chunk_material"))
    table("rag_nodes",
        sa.Column("retrieval_version_id", sa.String(128), sa.ForeignKey("rag_retrieval_versions.retrieval_version_id"), primary_key=True),
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("parent_node_id", sa.String(128), nullable=True),
        sa.Column("chunk_id", sa.String(128), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.ForeignKeyConstraint(["retrieval_version_id", "parent_node_id"], ["rag_nodes.retrieval_version_id", "rag_nodes.node_id"], name="fk_rag_node_parent"),
        sa.ForeignKeyConstraint(["retrieval_version_id", "chunk_id"], ["rag_chunks.retrieval_version_id", "rag_chunks.chunk_id"], name="fk_rag_node_chunk"))
    table("rag_chunk_spans",
        sa.Column("retrieval_version_id", sa.String(128), primary_key=True),
        sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.Column("ordinal", sa.Integer, primary_key=True),
        sa.Column("material_version_id", sa.String(36), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("start", sa.Integer, nullable=False),
        sa.Column("end", sa.Integer, nullable=False, quote=True),
        sa.Column("page", sa.Integer, nullable=True),
        sa.Column("block", sa.String(255), nullable=True),
        sa.ForeignKeyConstraint(["retrieval_version_id", "chunk_id", "material_version_id"], ["rag_chunks.retrieval_version_id", "rag_chunks.chunk_id", "rag_chunks.material_version_id"], name="fk_rag_span_chunk_material"),
        sa.CheckConstraint(sa.and_(sa.column("start") >= 0, sa.column(sa.sql.quoted_name("end", True)) > sa.column("start")), name="ck_rag_chunk_span_range"),
        sa.CheckConstraint("ordinal >= 0", name="ck_rag_chunk_span_ordinal"))
    table("rag_scope_snapshots",
        sa.Column("scope_snapshot_id", sa.String(128), primary_key=True),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), nullable=False),
        sa.Column("scope_version", sa.Integer, nullable=False),
        sa.Column("bindings", sa.JSON, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.UniqueConstraint("scope_snapshot_id", "space_id", name="uq_rag_scope_space"),
        sa.CheckConstraint("scope_version >= 0", name="ck_rag_scope_version"))
    table("rag_scope_spans",
        sa.Column("scope_snapshot_id", sa.String(128), sa.ForeignKey("rag_scope_snapshots.scope_snapshot_id"), primary_key=True),
        sa.Column("ordinal", sa.Integer, primary_key=True),
        sa.Column("material_version_id", sa.String(36), sa.ForeignKey("material_versions.id"), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("start", sa.Integer, nullable=False),
        sa.Column("end", sa.Integer, nullable=False, quote=True),
        sa.Column("page", sa.Integer, nullable=True),
        sa.Column("block", sa.String(255), nullable=True),
        sa.CheckConstraint(sa.and_(sa.column("start") >= 0, sa.column(sa.sql.quoted_name("end", True)) > sa.column("start")), name="ck_rag_scope_span_range"),
        sa.CheckConstraint("ordinal >= 0", name="ck_rag_scope_span_ordinal"))
    table("rag_scope_chunk_map",
        sa.Column("scope_snapshot_id", sa.String(128), sa.ForeignKey("rag_scope_snapshots.scope_snapshot_id"), primary_key=True),
        sa.Column("retrieval_version_id", sa.String(128), primary_key=True),
        sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.ForeignKeyConstraint(["retrieval_version_id", "chunk_id"], ["rag_chunks.retrieval_version_id", "rag_chunks.chunk_id"], name="fk_rag_scope_chunk"))
    table("rag_manifests",
        sa.Column("retrieval_version_id", sa.String(128), sa.ForeignKey("rag_retrieval_versions.retrieval_version_id"), primary_key=True),
        sa.Column("manifest_id", sa.String(128), primary_key=True),
        sa.Column("scope_snapshot_id", sa.String(128), nullable=False),
        sa.Column("space_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expected_counts", sa.JSON, nullable=False),
        sa.Column("actual_counts", sa.JSON, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.ForeignKeyConstraint(["scope_snapshot_id", "space_id"], ["rag_scope_snapshots.scope_snapshot_id", "rag_scope_snapshots.space_id"], name="fk_rag_manifest_scope_space"),
        sa.UniqueConstraint("retrieval_version_id", "manifest_id", "scope_snapshot_id", "space_id", name="uq_rag_manifest_publication"))
    table("rag_publications",
        sa.Column("space_id", sa.String(36), sa.ForeignKey("learning_spaces.id"), primary_key=True),
        sa.Column("material_version_id", sa.String(36), primary_key=True),
        sa.Column("retrieval_version_id", sa.String(128), nullable=False),
        sa.Column("manifest_id", sa.String(128), nullable=False),
        sa.Column("scope_snapshot_id", sa.String(128), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(["retrieval_version_id", "material_version_id"], ["rag_retrieval_versions.retrieval_version_id", "rag_retrieval_versions.material_version_id"], name="fk_rag_publication_material"),
        sa.ForeignKeyConstraint(["retrieval_version_id", "manifest_id", "scope_snapshot_id", "space_id"], ["rag_manifests.retrieval_version_id", "rag_manifests.manifest_id", "rag_manifests.scope_snapshot_id", "rag_manifests.space_id"], name="fk_rag_publication_manifest"),
        sa.CheckConstraint("generation >= 0", name="ck_rag_publication_generation"))
    return tables


TABLES = {table.name: table for table in _tables(Base.metadata)}
