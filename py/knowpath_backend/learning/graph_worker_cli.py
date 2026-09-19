"""Compatibility entrypoint for the durable graph worker."""

from .workers.graph_cli import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
