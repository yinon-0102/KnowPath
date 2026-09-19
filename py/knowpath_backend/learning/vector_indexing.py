"""Compatibility entrypoint for explicit vector-index maintenance."""

from .rag.indexing import index_material_version, main

__all__ = ["index_material_version", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
