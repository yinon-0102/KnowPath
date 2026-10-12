"""Load the repository's local environment consistently from any cwd."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def project_env_path() -> Path:
    """Return the backend environment file, independent of the process cwd."""
    return Path(__file__).resolve().parents[2] / ".env"


def load_project_env(path: str | Path | None = None) -> Path | None:
    """Load ``py/.env`` while preserving explicitly supplied environment values.

    The API and workers are often launched from either the repository root or
    ``py``.  ``python-dotenv``'s implicit cwd lookup is therefore not enough:
    it silently skips ``py/.env`` when the caller is at the repository root.
    ``override=False`` keeps process-level secrets and deployment settings
    authoritative over local defaults.
    """
    env_path = Path(path) if path is not None else project_env_path()
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)
        return env_path
    # Keep the normal dotenv fallback for packaged deployments that provide a
    # cwd-local .env instead of the source checkout's py/.env.
    load_dotenv(override=False)
    return None
