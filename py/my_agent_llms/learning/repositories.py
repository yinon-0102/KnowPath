"""SQLAlchemy repositories implementing the material domain port."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import IdempotencyRow, MaterialRow, MaterialVersionRow, SourceChunkRow
from .materials import IdempotencyConflict, Material, MaterialVersion, SourceChunk
from .unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyMaterialRepository:
    def __init__(self, engine, *, unit_of_work=None) -> None:
        self.unit_of_work = unit_of_work or SqlAlchemyUnitOfWork(engine)

    def transaction(self):
        return self.unit_of_work.transaction()

    def get_idempotency(self, key: str) -> tuple[str, str, str] | None:
        with self.unit_of_work.session() as session:
            row = session.get(IdempotencyRow, key)
            if row is None:
                return None
            if row.resource_type != "material_version" or row.version_id is None:
                raise IdempotencyConflict("idempotency key was reused for another operation")
            return row.request_fingerprint, row.resource_id, row.version_id

    def get_idempotency_run(self, key: str) -> str | None:
        with self.unit_of_work.session() as session:
            query = select(IdempotencyRow).where(IdempotencyRow.key == key)
            if self.unit_of_work.active:
                # Read the winner's committed association, even if an earlier
                # lookup established a MySQL REPEATABLE READ snapshot.
                query = query.with_for_update().execution_options(populate_existing=True)
            row = session.scalar(query)
            return row.run_id if row is not None else None

    def bind_idempotency_run(self, key: str, run_id: str) -> None:
        with self.unit_of_work.session() as session:
            row = session.scalar(select(IdempotencyRow).where(IdempotencyRow.key == key).with_for_update())
            if row is None:
                raise ValueError("cannot attach run to missing idempotency record")
            if row.run_id is not None and row.run_id != run_id:
                raise ValueError("idempotency run is already bound")
            row.run_id = run_id

    def get_material(self, material_id: str) -> Material | None:
        with self.unit_of_work.session() as session:
            query = select(MaterialRow).where(MaterialRow.id == material_id)
            if self.unit_of_work.active:
                query = query.with_for_update()
            row = session.scalar(query)
            return _material_from_row(row) if row else None

    def get_version(self, version_id: str) -> MaterialVersion | None:
        with self.unit_of_work.session() as session:
            query = select(MaterialVersionRow).where(MaterialVersionRow.id == version_id)
            if self.unit_of_work.active:
                query = query.with_for_update().execution_options(populate_existing=True)
            row = session.scalar(query)
            return _version_from_rows(row, session, lock=self.unit_of_work.active) if row else None

    def find_by_content_hash(self, content_hash: str) -> tuple[Material, MaterialVersion] | None:
        with self.unit_of_work.session() as session:
            query = select(MaterialVersionRow).where(MaterialVersionRow.content_hash == content_hash)
            if self.unit_of_work.active:
                # InnoDB's indexed next-key lock protects both existing hashes
                # and absent ranges until this ingestion commits.
                query = query.with_for_update()
            version = session.scalar(query)
            if version is None:
                return None
            material = session.scalar(select(MaterialRow).where(MaterialRow.id == version.material_id).with_for_update())
            if material is None:
                return None
            return _material_from_row(material), _version_from_rows(version, session, lock=self.unit_of_work.active)

    def save(self, material: Material, version: MaterialVersion, *, idempotency_key: str, request_fingerprint: str) -> None:
        with self.unit_of_work.session() as session:
            session.merge(_material_row(material))
            session.flush()
            session.merge(_version_row(version))
            session.flush()
            session.query(SourceChunkRow).filter(SourceChunkRow.material_version_id == version.id).delete()
            session.add_all(_chunk_row(version.id, chunk) for chunk in version.chunks)
            session.add(IdempotencyRow(
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="material_version",
                resource_id=material.id,
                version_id=version.id,
                created_at=datetime.now(timezone.utc),
            ))

    def record_idempotency(self, *, key: str, request_fingerprint: str, material_id: str, version_id: str) -> None:
        with self.unit_of_work.session() as session:
            session.add(IdempotencyRow(
                key=key,
                request_fingerprint=request_fingerprint,
                resource_type="material_version",
                resource_id=material_id,
                version_id=version_id,
                created_at=datetime.now(timezone.utc),
            ))

    def list_materials(self) -> list[Material]:
        with self.unit_of_work.session() as session:
            return [_material_from_row(row) for row in session.scalars(select(MaterialRow)).all()]

    def list_versions(self, material_id: str) -> list[MaterialVersion]:
        with self.unit_of_work.session() as session:
            rows = session.scalars(select(MaterialVersionRow).where(MaterialVersionRow.material_id == material_id)).all()
            return [_version_from_rows(row, session, lock=self.unit_of_work.active) for row in rows]

    def delete_material(self, material_id: str) -> None:
        with self.unit_of_work.session() as session:
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
        created_at=row.created_at.replace(tzinfo=timezone.utc),
        updated_at=row.updated_at.replace(tzinfo=timezone.utc),
    )


def _version_from_rows(row: MaterialVersionRow, session: Session, *, lock: bool = False) -> MaterialVersion:
    query = select(SourceChunkRow).where(SourceChunkRow.material_version_id == row.id)
    if lock:
        # A locking hash lookup may find a version newer than the transaction's
        # consistent-read snapshot. Its chunks must use a current read as well.
        query = query.with_for_update()
    chunks = session.scalars(query).all()
    return MaterialVersion(
        id=row.id,
        material_id=row.material_id,
        filename=row.filename,
        media_type=row.media_type,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        status=row.status,
        created_at=row.created_at.replace(tzinfo=timezone.utc),
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
