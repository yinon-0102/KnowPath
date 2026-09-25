"""Explicit RAG build/validation/publication; never migrates a database implicitly."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal


class BuildConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    space_id: str = Field(min_length=1)
    material_version_id: str = Field(min_length=1)
    max_tokens: int = Field(default=1800, ge=128, le=6000, strict=True)
    retry: bool = False
    plugin: Literal['a', 'b1', 'b15'] = 'a'
    collection_prefix: str = Field(default="knowpath_rag_content", pattern=r"^[a-zA-Z0-9_-]{1,70}$")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--config", required=True)
    build.add_argument("--output", required=True)
    for name in ("validate", "publish", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", required=True)
        if name != "validate":
            command.add_argument("--expected-generation", required=True, type=int)
    args = parser.parse_args(argv)
    engine = client = plugin = None
    try:
        if args.command == "build":
            config = BuildConfiguration.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
            manifest = None
        else:
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not all(isinstance(manifest.get(k), str) for k in
                ("retrieval_version_id", "manifest_id", "space_id", "material_version_id")):
                raise ValueError("invalid manifest identity")
            config = BuildConfiguration(space_id=manifest["space_id"], material_version_id=manifest["material_version_id"])
        from dotenv import load_dotenv
        from qdrant_client import QdrantClient
        from ..config import LearningSettings
        from ..persistence.db import create_db_engine
        from ..persistence.material_repository import SqlAlchemyMaterialRepository
        from ..persistence.rag_repository import SqlRagRepository
        from ..providers.models import embedding_model
        from ..state import LearningState
        from .building import RagBuilder
        from .registry import create_plugin
        from .vector import QdrantContentIndex
        load_dotenv()
        settings = LearningSettings.from_env()
        engine = create_db_engine()
        repo = SqlRagRepository(engine)
        materials = SqlAlchemyMaterialRepository(engine)
        state = LearningState(materials)
        embedder = embedding_model(settings)
        client = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
            api_key=os.getenv("QDRANT_API_KEY") or None, timeout=30, check_compatibility=False)
        embedding_profile = {"provider": settings.embedding_provider, "model": embedder.model_version,
            "dimension": embedder.dimension, "endpoint": settings.embedding_base_url}
        prefix = config.collection_prefix
        if manifest is not None:
            stored = repo.get_manifest(manifest["retrieval_version_id"], manifest["manifest_id"])
            if stored is None:
                raise ValueError("manifest does not exist")
            # Manifest files are only identity handles. All configuration comes
            # from SQL; never let edited JSON redirect publication to a new index.
            manifest = stored
            prefix = stored["configuration"]["collection_prefix"]
        dense = QdrantContentIndex(client, prefix, embedder.dimension, embedding_profile)
        builder = RagBuilder(repo, materials, state.space_service, embedder, dense)
        if manifest is not None and dense.collection != manifest["configuration"]["vector_collection"]:
            raise ValueError("embedding profile differs from frozen manifest")
        if args.command == "build":
            # Only this explicit command receives build authority. Request-time
            # plugins remain retrieval-only and never create or publish indexes.
            plugin = create_plugin(config.plugin, embedder, dense, builder=builder)
            scope = state.space_service.rag_scope_snapshot(config.space_id)
            result = plugin.prepare(scope, {"max_tokens": config.max_tokens},
                {"material_version_id": config.material_version_id, "retry": config.retry})
            # Write only after a successful build; failures cannot masquerade as
            # publishable output left over from a partially written JSON file.
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"status": result["status"], "manifest_id": result["manifest_id"]}))
        elif args.command == "validate":
            builder.validate(manifest)
            print(json.dumps({"status": "verified", "manifest_id": manifest["manifest_id"]}))
        else:
            # Rollback is another guarded publication of an older READY manifest,
            # not a SQL downgrade or restoration of historical visibility.
            generation = builder.publish(manifest, expected_generation=args.expected_generation)
            print(json.dumps({"status": "published", "generation": generation}))
        return 0
    except (ValueError, ValidationError, OSError, json.JSONDecodeError):
        print("RAG_CONFIG_INVALID", file=sys.stderr)
        return 1
    except Exception:
        print("RAG_OPERATION_FAILED", file=sys.stderr)
        return 1
    finally:
        if plugin is not None:
            plugin.close()
        if client is not None:
            client.close()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
