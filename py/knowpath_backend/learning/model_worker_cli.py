"""Compatibility entrypoint for the durable model worker."""

from .workers.model_cli import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
