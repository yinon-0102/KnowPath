"""Create the configured learning database schema through the package entrypoint."""

from knowpath_backend.learning.db_cli import main


if __name__ == "__main__":
    main()
