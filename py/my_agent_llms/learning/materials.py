"""Material ingestion primitives.

The service deliberately owns content hashing, source chunk creation and
idempotency.  HTTP handlers can therefore remain thin and future persistent
repositories can implement the same small repository contract.
"""

from __future__ import annotations

import copy
import hashlib
from contextlib import contextmanager
from threading import RLock
import mimetypes
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Protocol
from uuid import uuid4

from .runs import InMemoryRunRepository
from .errors import DomainConflict, DomainNotFound
from .material_schemas import UpdateMaterial


class MaterialError(Exception):
    """Base error for material ingestion failures."""


class UnsupportedMaterial(MaterialError):
    """The file extension or media type is outside the first-release scope."""


class MaterialParseError(MaterialError):
    """The input has a supported type but cannot be parsed into text."""


class IdempotencyConflict(MaterialError):
    """An idempotency key was reused with a different request payload."""


class MaterialNotFound(MaterialError):
    """The requested material does not exist."""


@dataclass(frozen=True)
class SourceChunk:
    id: str
    text: str
    section_path: tuple[str, ...]
    line_start: int | None
    line_end: int | None
    page: int | None
    content_hash: str


@dataclass
class MaterialVersion:
    id: str
    material_id: str
    filename: str
    media_type: str
    content_hash: str
    size_bytes: int
    status: str
    created_at: datetime
    chunks: list[SourceChunk] = field(default_factory=list)


@dataclass
class Material:
    id: str
    name: str
    type: str
    status: str
    current_version_id: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime
    version: int = 1


@dataclass(frozen=True)
class CreateMaterialResult:
    material: Material
    version: MaterialVersion
    replayed: bool = False


class MaterialRepository(Protocol):
    def transaction(self): ...

    def get_idempotency_run(self, key: str) -> str | None: ...

    def bind_idempotency_run(self, key: str, run_id: str) -> None: ...

    def get_idempotency(self, key: str) -> tuple[str, str, str] | None: ...

    def get_material(self, material_id: str) -> Material | None: ...

    def get_version(self, version_id: str) -> MaterialVersion | None: ...

    def find_by_content_hash(self, content_hash: str) -> tuple[Material, MaterialVersion] | None: ...

    def save(
        self,
        material: Material,
        version: MaterialVersion,
        *,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> None: ...

    def record_idempotency(
        self,
        *,
        key: str,
        request_fingerprint: str,
        material_id: str,
        version_id: str,
    ) -> None: ...

    def update_material(self, material: Material) -> None: ...

    def delete_material(self, material_id: str) -> None: ...

    def list_materials(self) -> list[Material]: ...

    def list_versions(self, material_id: str) -> list[MaterialVersion]: ...


class InMemoryMaterialRepository:
    """Transactional memory adapter for local use and contract tests."""

    def __init__(self) -> None:
        self.run_repository = InMemoryRunRepository()
        self.materials: dict[str, Material] = {}
        self.versions: dict[str, MaterialVersion] = {}
        self.by_content_hash: dict[str, tuple[str, str]] = {}
        self.idempotency: dict[str, tuple[str, str, str | None]] = {}
        self.idempotency_runs: dict[str, str] = {}
        self.idempotency_responses: dict[str, dict] = {}
        self.learning_spaces: dict[str, dict] = {}
        self.assessment_data = {"assessments": {}, "attempts": {}, "states": {}, "evidence": {}, "resets": {}, "plans": {}, "tasks": {}, "sessions": {}, "session_events": {}}
        self.planner_idempotency: dict[str, tuple[str, str, dict]] = {}
        self.export_data: dict[str, dict] = {}
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            previous = copy.deepcopy((self.materials, self.versions, self.by_content_hash,
                                      self.idempotency, self.idempotency_runs, self.idempotency_responses, self.learning_spaces, self.assessment_data, self.planner_idempotency, self.export_data))
            try:
                yield
            except BaseException:
                (self.materials, self.versions, self.by_content_hash,
                 self.idempotency, self.idempotency_runs, self.idempotency_responses, self.learning_spaces, self.assessment_data, self.planner_idempotency, self.export_data) = previous
                raise

    def get_idempotency(self, key: str) -> tuple[str, str, str] | None:
        with self._lock:
            record = self.idempotency.get(key)
            if record is not None and record[2] is None:
                raise IdempotencyConflict("idempotency key was reused for another operation")
            return record

    def get_idempotency_run(self, key: str) -> str | None:
        with self._lock:
            return self.idempotency_runs.get(key)

    def bind_idempotency_run(self, key: str, run_id: str) -> None:
        with self._lock:
            if key not in self.idempotency:
                raise ValueError("cannot attach run to missing idempotency record")
            previous = self.idempotency_runs.get(key)
            if previous is not None and previous != run_id:
                raise ValueError("idempotency run is already bound")
            self.idempotency_runs[key] = run_id

    def get_material(self, material_id: str) -> Material | None:
        with self._lock:
            return copy.deepcopy(self.materials.get(material_id))

    def get_version(self, version_id: str) -> MaterialVersion | None:
        with self._lock:
            return copy.deepcopy(self.versions.get(version_id))

    def find_by_content_hash(self, content_hash: str) -> tuple[Material, MaterialVersion] | None:
        with self._lock:
            ids = self.by_content_hash.get(content_hash)
            if ids is None:
                return None
            material = self.materials.get(ids[0])
            version = self.versions.get(ids[1])
            return copy.deepcopy((material, version)) if material and version else None

    def save(self, material: Material, version: MaterialVersion, *,
             idempotency_key: str, request_fingerprint: str) -> None:
        with self._lock:
            self.materials[material.id] = copy.deepcopy(material)
            self.versions[version.id] = copy.deepcopy(version)
            self.by_content_hash[version.content_hash] = (material.id, version.id)
            self.idempotency[idempotency_key] = (request_fingerprint, material.id, version.id)

    def record_idempotency(self, *, key: str, request_fingerprint: str,
                           material_id: str, version_id: str) -> None:
        with self._lock:
            self.idempotency[key] = (request_fingerprint, material_id, version_id)

    def delete_material(self, material_id: str) -> None:
        with self._lock:
            self.materials.pop(material_id, None)
            self.versions = {key: value for key, value in self.versions.items()
                             if value.material_id != material_id}
            self.by_content_hash = {key: value for key, value in self.by_content_hash.items()
                                    if value[0] != material_id}

    def update_material(self, material: Material) -> None:
        with self._lock:
            self.materials[material.id] = copy.deepcopy(material)

    def list_materials(self) -> list[Material]:
        with self._lock:
            return copy.deepcopy(list(self.materials.values()))

    def list_versions(self, material_id: str) -> list[MaterialVersion]:
        with self._lock:
            return copy.deepcopy([v for v in self.versions.values() if v.material_id == material_id])


class MaterialParser:
    """Parse supported files into source chunks with stable source locations."""

    SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf"}
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    def parse(self, content: bytes, *, filename: str) -> list[SourceChunk]:
        extension = PurePath(filename).suffix.lower()
        if extension not in self.SUPPORTED_EXTENSIONS:
            raise UnsupportedMaterial(f"unsupported extension: {extension or '<none>'}")
        if not content:
            raise MaterialParseError("material is empty")

        if extension == ".pdf":
            return self._parse_pdf(content)
        else:
            try:
                text = content.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError as exc:
                raise MaterialParseError("material is not valid UTF-8") from exc
        return self._parse_text(text)

    def _parse_pdf(self, content: bytes) -> list[SourceChunk]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise MaterialParseError("PDF parser is not installed") from exc

        try:
            import io

            reader = PdfReader(io.BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # pragma: no cover - parser-specific failures
            raise MaterialParseError("failed to parse PDF") from exc
        if not any(text.strip() for text in pages):
            raise MaterialParseError("PDF has no extractable text")
        chunks: list[SourceChunk] = []
        for page_number, text in enumerate(pages, start=1):
            if not text.strip():
                continue
            chunks.extend(replace(chunk, page=page_number) for chunk in self._parse_text(text))
        return chunks

    def _parse_text(self, text: str) -> list[SourceChunk]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.splitlines()
        chunks: list[SourceChunk] = []
        headings: list[str] = []
        paragraph: list[tuple[int, str]] = []

        def flush() -> None:
            if not paragraph:
                return
            body = "\n".join(line for _, line in paragraph).strip()
            if not body:
                paragraph.clear()
                return
            start = next(index for index, line in paragraph if line.strip())
            end = next(index for index, line in reversed(paragraph) if line.strip())
            chunks.append(
                SourceChunk(
                    id=str(uuid4()),
                    text=body,
                    section_path=tuple(headings),
                    line_start=start,
                    line_end=end,
                    page=None,
                    content_hash=_sha256(body.encode("utf-8")),
                )
            )
            paragraph.clear()

        for number, line in enumerate(lines, start=1):
            heading = self._HEADING_RE.match(line)
            if heading:
                flush()
                level = len(heading.group(1))
                headings[:] = headings[: level - 1]
                headings.append(heading.group(2).strip())
            elif line.strip():
                paragraph.append((number, line))
            else:
                flush()
        flush()

        if not chunks:
            raise MaterialParseError("material contains no extractable text")
        return chunks


class MaterialService:
    def __init__(self, repository: MaterialRepository, parser: MaterialParser | None = None) -> None:
        self.repository = repository
        self.parser = parser or MaterialParser()

    def update(self, material_id, payload):
        payload = UpdateMaterial.model_validate(payload).model_dump(exclude_unset=True)
        with self.repository.transaction():
            material = self.repository.get_material(material_id)
            if material is None:
                raise DomainNotFound("material", material_id)
            if payload["expected_version"] != material.version:
                raise DomainConflict("VERSION_CONFLICT", "资料版本已变化，请刷新后重试", {"current_version": material.version})
            for field in ("name", "status"):
                if field in payload:
                    setattr(material, field, payload[field])
            material.version += 1
            material.updated_at = datetime.now(timezone.utc)
            self.repository.update_material(material)
            return material

    def create(self, *, filename: str, content: bytes, idempotency_key: str, name: str | None = None) -> CreateMaterialResult:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        content_hash = _sha256(content)
        request_fingerprint = _sha256(
            f"{filename}\0{name or ''}\0{content_hash}".encode("utf-8")
        )
        previous = self.repository.get_idempotency(idempotency_key)
        if previous is not None:
            previous_fingerprint, material_id, version_id = previous
            if previous_fingerprint != request_fingerprint:
                raise IdempotencyConflict("idempotency key was reused with a different request")
            material = self.repository.get_material(material_id)
            version = self.repository.get_version(version_id)
            if material is None or version is None:
                raise MaterialError("idempotency record points to missing material")
            return CreateMaterialResult(material=material, version=version, replayed=True)

        chunks = self.parser.parse(content, filename=filename)
        duplicate = self.repository.find_by_content_hash(content_hash)
        if duplicate is not None:
            material, version = duplicate
            self.repository.record_idempotency(
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                material_id=material.id,
                version_id=version.id,
            )
            return CreateMaterialResult(material=material, version=version, replayed=True)

        now = datetime.now(timezone.utc)
        material_id = str(uuid4())
        version_id = str(uuid4())
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        material = Material(
            id=material_id,
            name=name or PurePath(filename).stem or "未命名资料",
            type=PurePath(filename).suffix.lower().lstrip(".") or "unknown",
            status="ready",
            current_version_id=version_id,
            size_bytes=len(content),
            created_at=now,
            updated_at=now,
        )
        version = MaterialVersion(
            id=version_id,
            material_id=material_id,
            filename=filename,
            media_type=media_type,
            content_hash=content_hash,
            size_bytes=len(content),
            status="ready",
            created_at=now,
            chunks=chunks,
        )
        self.repository.save(
            material,
            version,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        return CreateMaterialResult(material=material, version=version)

    def create_version(
        self,
        *,
        material_id: str,
        filename: str,
        content: bytes,
        idempotency_key: str,
    ) -> CreateMaterialResult:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        material = self.repository.get_material(material_id)
        if material is None:
            raise MaterialNotFound(material_id)
        content_hash = _sha256(content)
        request_fingerprint = _sha256(
            f"{material_id}\0{filename}\0{content_hash}".encode("utf-8")
        )
        previous = self.repository.get_idempotency(idempotency_key)
        if previous is not None:
            previous_fingerprint, previous_material_id, version_id = previous
            if previous_fingerprint != request_fingerprint:
                raise IdempotencyConflict("idempotency key was reused with a different request")
            version = self.repository.get_version(version_id)
            if version is None or previous_material_id != material_id:
                raise MaterialError("idempotency record points to missing version")
            return CreateMaterialResult(material=material, version=version, replayed=True)

        chunks = self.parser.parse(content, filename=filename)
        duplicate = self.repository.find_by_content_hash(content_hash)
        if duplicate is not None and duplicate[0].id == material_id:
            self.repository.record_idempotency(
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                material_id=material_id,
                version_id=duplicate[1].id,
            )
            return CreateMaterialResult(material=material, version=duplicate[1], replayed=True)

        now = datetime.now(timezone.utc)
        version = MaterialVersion(
            id=str(uuid4()),
            material_id=material_id,
            filename=filename,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            content_hash=content_hash,
            size_bytes=len(content),
            status="ready",
            created_at=now,
            chunks=chunks,
        )
        material.current_version_id = version.id
        material.version += 1
        material.size_bytes = len(content)
        material.updated_at = now
        self.repository.save(
            material,
            version,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        return CreateMaterialResult(material=material, version=version)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
