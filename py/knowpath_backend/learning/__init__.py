"""Learning-domain services for the KnowPath Web backend."""

from .materials import (
    CreateMaterialResult,
    IdempotencyConflict,
    InMemoryMaterialRepository,
    Material,
    MaterialNotFound,
    MaterialParseError,
    MaterialParser,
    MaterialService,
    MaterialVersion,
    MaterialError,
    SourceChunk,
    UnsupportedMaterial,
)
from .config import LearningSettings
from .state import DomainConflict, DomainNotFound, LearningState
from .storage import GraphSettings, VectorSettings

try:  # Database adapters are optional for the in-memory local slice.
    from knowpath_backend.learning.persistence.db import Base, DatabaseSettings, create_db_engine, init_db
    from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
except ImportError:  # pragma: no cover - exercised only before dependency install
    Base = DatabaseSettings = create_db_engine = init_db = None
    SqlAlchemyMaterialRepository = None

from .indexes import Neo4jGraphStore, QdrantVectorStore

__all__ = [
    "CreateMaterialResult",
    "IdempotencyConflict",
    "InMemoryMaterialRepository",
    "Material",
    "MaterialNotFound",
    "MaterialParseError",
    "MaterialParser",
    "MaterialService",
    "MaterialVersion",
    "MaterialError",
    "SourceChunk",
    "UnsupportedMaterial",
    "LearningSettings",
    "DomainConflict",
    "DomainNotFound",
    "LearningState",
    "Base",
    "DatabaseSettings",
    "create_db_engine",
    "init_db",
    "GraphSettings",
    "VectorSettings",
    "SqlAlchemyMaterialRepository",
    "Neo4jGraphStore",
    "QdrantVectorStore",
]
