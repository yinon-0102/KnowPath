"""CLI entry point for initializing the configured learning database."""

from .db import create_db_engine, init_db


def main() -> None:
    init_db(create_db_engine())
    print("learning database schema initialized")
