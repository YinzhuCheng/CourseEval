import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import ensure_data_directories, init_database


def main() -> None:
    ensure_data_directories()
    init_database()
    print("Database initialized.")


if __name__ == "__main__":
    main()
