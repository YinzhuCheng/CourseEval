import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.auth import assign_user_role
from app.constants import UserRole
from app.db import SessionLocal, ensure_data_directories, init_database, utcnow
from app.models import User


def main(username_or_email: str) -> None:
    ensure_data_directories()
    init_database()
    with SessionLocal() as db:
        user = (
            db.query(User)
            .filter((User.username == username_or_email) | (User.email == username_or_email))
            .first()
        )
        if user is None:
            raise SystemExit(f"User not found: {username_or_email}")
        assign_user_role(user, UserRole.SUPER_ADMIN)
        user.updated_at = utcnow()
        db.commit()
        print(f"Promoted {user.username} to super_admin.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/init_super_admin.py <username-or-email>")
    main(sys.argv[1])
