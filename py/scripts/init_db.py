"""Create the configured learning database schema."""

from my_agent_llms.learning.db_cli import main


def main() -> None:
    init_db(create_db_engine())
    print("learning database schema initialized")


if __name__ == "__main__":
    main()
