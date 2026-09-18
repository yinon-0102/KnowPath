"""SQLAlchemy repositories implementing the material domain port."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db import IdempotencyRow, MaterialRow, MaterialVersionRow, SourceChunkRow
from .materials import Material, MaterialVersion, SourceChunk


class SqlAlchemyMaterialRepository:
    def __init__(self, engine) -> None:
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def get_idempotency(self, key: str) -> tuple[str, str, str] | None:
        with self._session_factory() as session:
            row = session.get(IdempotencyRow, key)
            if row is None or row.version_id is None:
                return None
            return row.request_fingerprint, row.resource_id, row.version_id

    def get_material(self, material_id: str) -> Material | None:
        with self._session_factory() as session:
            row = session.get(MaterialRow, material_id)
            return _material_from_row(row) if row else None

    def get_version(self, version_id: str) -> MaterialVersion | None:
        with self._session_factory() as session:
            row = session.get(MaterialVersionRow, version_id)
            return _version_from_rows(row, session) if row else None

    def find_by_content_hash(self, content_hash: str) -> tuple[Material, MaterialVersion] | None:
        with self._session_factory() as session:
            version = session.scalar(select(MaterialVersionRow).where(MaterialVersionRow.content_hash == content_hash))
            if version is None:
                return None
            material = session.get(MaterialRow, version.material_id)
            if material is None:
                return None
            return _material_from_row(material), _version_from_rows(version, session)

    def save(self, material: Material, version: MaterialVersion, *, idempotency_key: str, request_fingerprint: str) -> None:
        with self._session_factory.begin() as session:
            session.merge(_material_row(material))
            session.merge(_version_row(version))
            session.query(SourceChunkRow).filter(SourceChunkRow.material_version_id == version.id).delete()
            session.add_all(_chunk_row(version.id, chunk) for chunk in version.chunks)
            session.merge(IdempotencyRow(
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="material_version",
                resource_id=material.id,
                version_id=version.id,
                created_at=datetime.now(timezone.utc),
            ))

    def record_idempotency(self, *, key: str, request_fingerprint: str, material_id: str, version_id: str) -> None:
        with self._session_factory.begin() as session:
            session.merge(IdempotencyRow(
                key=key,
                request_fingerprint=request_fingerprint,
                resource_type="material_version",
                resource_id=material_id,
                version_id=version_id,
                created_at=datetime.now(timezone.utc),
            ))

    def list_materials(self) -> list[Material]:
        with self._session_factory() as session:
            return [_material_from_row(row) for row in session.scalars(select(MaterialRow)).all()]

    def list_versions(self, material_id: str) -> list[MaterialVersion]:
        with self._session_factory() as session:
            rows = session.scalars(select(MaterialVersionRow).where(MaterialVersionRow.material_id == material_id)).all()
            return [_version_from_rows(row, session) for row in rows]

    def delete_material(self, material_id: str) -> None:
        with self._session_factory.begin() as session:
            session.query(SourceChunkRow).filter(SourceChunkRow.material_version_id.in_(select(MaterialVersionRow.id).where(MaterialVersionRow.material_id == material_id))).delete(synchronize_session=False)
            session.query(MaterialVersionRow).filter(MaterialVersionRow.material_id == material_id).delete(synchronize_session=False)
            session.query(MaterialRow).filter(MaterialRow.id == material_id).delete(synchronize_session=False)


def _material_row(material: Material) -> MaterialRow:
    return MaterialRow(
        id=material.id,
        name=material.name,
        type=material.type,
        status=material.status,
        current_version_id=material.current_version_id,
        size_bytes=material.size_bytes,
        created_at=material.created_at,
        updated_at=material.updated_at,
    )


def _version_row(version: MaterialVersion) -> MaterialVersionRow:
    return MaterialVersionRow(
        id=version.id,
        material_id=version.material_id,
        filename=version.filename,
        media_type=version.media_type,
        content_hash=version.content_hash,
        size_bytes=version.size_bytes,
        status=version.status,
        created_at=version.created_at,
    )


def _chunk_row(version_id: str, chunk: SourceChunk) -> SourceChunkRow:
    return SourceChunkRow(
        id=chunk.id,
        material_version_id=version_id,
        text=chunk.text,
        section_path=list(chunk.section_path),
        page=chunk.page,
        line_start=chunk.line_start,
        line_end=chunk.line_end,
        content_hash=chunk.content_hash,
    )


def _material_from_row(row: MaterialRow) -> Material:
    return Material(
        id=row.id,
        name=row.name,
        type=row.type,
        status=row.status,
        current_version_id=row.current_version_id or "",
        size_bytes=row.size_bytes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _version_from_rows(row: MaterialVersionRow, session: Session) -> MaterialVersion:
    chunks = session.scalars(select(SourceChunkRow).where(SourceChunkRow.material_version_id == row.id)).all()
    return MaterialVersion(
        id=row.id,
        material_id=row.material_id,
        filename=row.filename,
        media_type=row.media_type,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        status=row.status,
        created_at=row.created_at,
        chunks=[
            SourceChunk(
                id=chunk.id,
                text=chunk.text,
                section_path=tuple(chunk.section_path or []),
                line_start=chunk.line_start,
                line_end=chunk.line_end,
                page=chunk.page,
                content_hash=chunk.content_hash,
            )
            for chunk in chunks
        ],
    )
