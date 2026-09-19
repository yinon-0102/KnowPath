"""Material ingestion primitives.

The service deliberately owns content hashing, source chunk creation and
idempotency.  HTTP handlers can therefore remain thin and future persistent
repositories can implement the same small repository contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from threading import RLock
import mimetypes
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Protocol
from uuid import uuid4

from knowpath_backend.learning.workers.runs import InMemoryRunRepository
from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.materials.schemas import UpdateMaterial


class MaterialError(Exception):
    """Base error for material ingestion failures."""


class UnsupportedMaterial(MaterialError):
    """The file extension or media type is outside the first-release scope."""


class MaterialTooLarge(MaterialError):
    """The uploaded file exceeds the supported size limit."""


class MaterialParseError(MaterialError):
    """The input has a supported type but cannot be parsed into text."""

    def __init__(self, message: str, *, code: str = "MATERIAL_PARSE_FAILED") -> None:
        super().__init__(message)
        self.code = code


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

    def find_by_content_hash(self, content_hash: str, *, filename: str | None = None, material_id: str | None = None) -> tuple[Material, MaterialVersion] | None: ...

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

    def save_raw(self, version_id: str, content: bytes) -> None: ...

    def get_raw(self, version_id: str) -> bytes | None: ...

    def update_version(self, version: MaterialVersion) -> None: ...

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
        self.raw_files: dict[str, bytes] = {}
        self.by_content_hash: dict[str, tuple[str, str]] = {}
        self.idempotency: dict[str, tuple[str, str, str | None]] = {}
        self.idempotency_runs: dict[str, str] = {}
        self.idempotency_responses: dict[str, dict] = {}
        self.learning_spaces: dict[str, dict] = {}
        self.assessment_data = {"assessments": {}, "attempts": {}, "states": {}, "evidence": {}, "resets": {}, "plans": {}, "tasks": {}, "sessions": {}, "session_events": {}, "conversations": {}, "messages": {}, "graph_revisions": {}, "outbox": {}, "corrections": {}}
        self.planner_idempotency: dict[str, tuple[str, str, dict]] = {}
        self.export_data: dict[str, dict] = {}
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            previous = copy.deepcopy((self.materials, self.versions, self.raw_files, self.by_content_hash,
                                      self.idempotency, self.idempotency_runs, self.idempotency_responses, self.learning_spaces, self.assessment_data, self.planner_idempotency, self.export_data))
            try:
                yield
            except BaseException:
                (self.materials, self.versions, self.raw_files, self.by_content_hash,
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

    def find_by_content_hash(self, content_hash: str, *, filename: str | None = None, material_id: str | None = None) -> tuple[Material, MaterialVersion] | None:
        with self._lock:
            for version in self.versions.values():
                if (version.content_hash == content_hash
                        and (filename is None or parser_kind(version.filename) == parser_kind(filename))
                        and (material_id is None or version.material_id == material_id)):
                    material = self.materials.get(version.material_id)
                    if material is not None:
                        return copy.deepcopy((material, version))
            return None

    def save(self, material: Material, version: MaterialVersion, *,
             idempotency_key: str, request_fingerprint: str) -> None:
        with self._lock:
            self.materials[material.id] = copy.deepcopy(material)
            self.versions[version.id] = copy.deepcopy(version)
            self.by_content_hash[version.content_hash] = (material.id, version.id)
            self.idempotency[idempotency_key] = (request_fingerprint, material.id, version.id)

    def save_raw(self, version_id: str, content: bytes) -> None:
        with self._lock:
            if version_id not in self.versions:
                raise MaterialNotFound(version_id)
            previous = self.raw_files.get(version_id)
            if previous is not None and previous != content:
                raise ValueError("raw material content is immutable")
            self.raw_files[version_id] = bytes(content)

    def get_raw(self, version_id: str) -> bytes | None:
        with self._lock:
            return self.raw_files.get(version_id)

    def update_version(self, version: MaterialVersion) -> None:
        with self._lock:
            if version.id not in self.versions:
                raise MaterialNotFound(version.id)
            stored = self.versions[version.id]
            self.versions[version.id] = replace(stored, status=version.status, chunks=copy.deepcopy(version.chunks))

    def record_idempotency(self, *, key: str, request_fingerprint: str,
                           material_id: str, version_id: str) -> None:
        with self._lock:
            self.idempotency[key] = (request_fingerprint, material_id, version_id)

    def delete_material(self, material_id: str) -> None:
        with self._lock:
            self.materials.pop(material_id, None)
            self.versions = {key: value for key, value in self.versions.items()
                             if value.material_id != material_id}
            self.raw_files = {key: value for key, value in self.raw_files.items()
                              if key in self.versions}
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
    MAX_SIZE_BYTES = 20 * 1024 * 1024
    MAX_PDF_PAGES = 300
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    @classmethod
    def validate_upload(cls, content: bytes, *, filename: str) -> None:
        extension = PurePath(filename).suffix.lower()
        if extension not in cls.SUPPORTED_EXTENSIONS:
            raise UnsupportedMaterial(f"unsupported extension: {extension or '<none>'}")
        if len(content) > cls.MAX_SIZE_BYTES:
            raise MaterialTooLarge("material exceeds the 20 MiB upload limit")

    def parse(self, content: bytes, *, filename: str) -> list[SourceChunk]:
        self.validate_upload(content, filename=filename)
        extension = PurePath(filename).suffix.lower()
        if extension not in self.SUPPORTED_EXTENSIONS:
            raise UnsupportedMaterial(f"unsupported extension: {extension or '<none>'}")
        if not content:
            raise MaterialParseError("material is empty", code="EMPTY_MATERIAL")

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
            if reader.is_encrypted:
                raise MaterialParseError("encrypted PDFs are unsupported", code="ENCRYPTED_PDF")
            if len(reader.pages) > self.MAX_PDF_PAGES:
                raise MaterialParseError("PDF exceeds the 300 page limit", code="PDF_PAGE_LIMIT_EXCEEDED")
            pages = [page.extract_text() or "" for page in reader.pages]
        except MaterialParseError:
            raise
        except Exception as exc:  # pragma: no cover - parser-specific failures
            raise MaterialParseError("failed to parse PDF") from exc
        if not any(text.strip() for text in pages):
            raise MaterialParseError("PDF has no extractable text", code="SCANNED_PDF_UNSUPPORTED")
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
            raise MaterialParseError("material contains no extractable text", code="EMPTY_MATERIAL")
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

    def create(self, *, filename: str, content: bytes, idempotency_key: str, name: str | None = None,
               defer_parse: bool = False, auto_ingest: bool | None = None) -> CreateMaterialResult:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        MaterialParser.validate_upload(content, filename=filename)
        content_hash = _sha256(content)
        request_fingerprint = _sha256(
            f"{filename}\0{name or ''}\0{content_hash}".encode("utf-8")
        )
        legacy_fingerprint = request_fingerprint
        request_fingerprint = _request_options_fingerprint(request_fingerprint, defer_parse=defer_parse, auto_ingest=auto_ingest)
        previous = self.repository.get_idempotency(idempotency_key)
        if previous is not None:
            previous_fingerprint, material_id, version_id = previous
            legacy_default = defer_parse and auto_ingest is True and previous_fingerprint == legacy_fingerprint
            if previous_fingerprint != request_fingerprint and not legacy_default:
                raise IdempotencyConflict("idempotency key was reused with a different request")
            material = self.repository.get_material(material_id)
            version = self.repository.get_version(version_id)
            if material is None or version is None:
                raise MaterialError("idempotency record points to missing material")
            self.repository.save_raw(version.id, content)
            return CreateMaterialResult(material=material, version=version, replayed=True)

        chunks = [] if defer_parse else self.parser.parse(content, filename=filename)
        duplicate = self.repository.find_by_content_hash(content_hash, filename=filename)
        if duplicate is not None:
            material, version = duplicate
            self.repository.record_idempotency(
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                material_id=material.id,
                version_id=version.id,
            )
            self.repository.save_raw(version.id, content)
            return CreateMaterialResult(material=material, version=version, replayed=True)

        now = datetime.now(timezone.utc)
        material_id = str(uuid4())
        version_id = str(uuid4())
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        material = Material(
            id=material_id,
            name=name or PurePath(filename).stem or "未命名资料",
            type=PurePath(filename).suffix.lower().lstrip(".") or "unknown",
            status="uploaded" if defer_parse else "ready",
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
            status="uploaded" if defer_parse else "ready",
            created_at=now,
            chunks=chunks,
        )
        self.repository.save(
            material,
            version,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        self.repository.save_raw(version.id, content)
        return CreateMaterialResult(material=material, version=version)

    def create_version(
        self,
        *,
        material_id: str,
        filename: str,
        content: bytes,
        idempotency_key: str,
        defer_parse: bool = False,
        auto_ingest: bool | None = None,
        change_note: str | None = None,
    ) -> CreateMaterialResult:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        material = self.repository.get_material(material_id)
        if material is None:
            raise MaterialNotFound(material_id)
        MaterialParser.validate_upload(content, filename=filename)
        content_hash = _sha256(content)
        request_fingerprint = _sha256(
            f"{material_id}\0{filename}\0{content_hash}".encode("utf-8")
        )
        legacy_fingerprint = request_fingerprint
        request_fingerprint = _request_options_fingerprint(
            request_fingerprint, defer_parse=defer_parse, auto_ingest=auto_ingest, change_note=change_note)
        previous = self.repository.get_idempotency(idempotency_key)
        if previous is not None:
            previous_fingerprint, previous_material_id, version_id = previous
            legacy_default = defer_parse and auto_ingest is True and change_note is None and previous_fingerprint == legacy_fingerprint
            if previous_fingerprint != request_fingerprint and not legacy_default:
                raise IdempotencyConflict("idempotency key was reused with a different request")
            version = self.repository.get_version(version_id)
            if version is None or previous_material_id != material_id:
                raise MaterialError("idempotency record points to missing version")
            self.repository.save_raw(version.id, content)
            return CreateMaterialResult(material=material, version=version, replayed=True)

        chunks = [] if defer_parse else self.parser.parse(content, filename=filename)
        duplicate = self.repository.find_by_content_hash(content_hash, filename=filename, material_id=material_id)
        if duplicate is not None and duplicate[0].id == material_id:
            self.repository.record_idempotency(
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                material_id=material_id,
                version_id=duplicate[1].id,
            )
            self.repository.save_raw(duplicate[1].id, content)
            return CreateMaterialResult(material=material, version=duplicate[1], replayed=True)

        now = datetime.now(timezone.utc)
        version = MaterialVersion(
            id=str(uuid4()),
            material_id=material_id,
            filename=filename,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            content_hash=content_hash,
            size_bytes=len(content),
            status="uploaded" if defer_parse else "ready",
            created_at=now,
            chunks=chunks,
        )
        material.current_version_id = version.id
        if defer_parse and material.status != "archived":
            material.status = "uploaded"
        material.version += 1
        material.size_bytes = len(content)
        material.updated_at = now
        self.repository.save(
            material,
            version,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        self.repository.save_raw(version.id, content)
        return CreateMaterialResult(material=material, version=version)


def _request_options_fingerprint(fingerprint: str, *, defer_parse: bool,
                                 auto_ingest: bool | None, change_note: str | None = None) -> str:
    if not defer_parse and auto_ingest is None and change_note is None:
        return fingerprint
    options = json.dumps({"defer_parse": defer_parse, "auto_ingest": auto_ingest,
                          "change_note": change_note}, sort_keys=True, separators=(",", ":"))
    return _sha256(f"{fingerprint}\0{options}".encode("utf-8"))


def parser_kind(filename: str) -> str:
    return "pdf" if PurePath(filename).suffix.lower() == ".pdf" else "text"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
