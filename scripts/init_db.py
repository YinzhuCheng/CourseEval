from app.db import ensure_data_directories, init_database


def main() -> None:
    ensure_data_directories()
    init_database()
    print("Database initialized.")


if __name__ == "__main__":
    main()
