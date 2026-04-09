import base64
import hashlib
import hmac
import secrets
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.constants import AccountRole, PlatformRole
from app.models import User


SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_LEN = 64


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_LEN,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_b64}${digest_b64}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, n_value, r_value, p_value, salt_b64, digest_b64 = password_hash.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected_digest = base64.b64decode(digest_b64.encode("ascii"))
        actual_digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_value),
            r=int(r_value),
            p=int(p_value),
            dklen=len(expected_digest),
        )
        return hmac.compare_digest(actual_digest, expected_digest)
    except Exception:
        return False


def find_user_by_login(db: Session, login: str) -> User | None:
    statement = select(User).where(or_(User.username == login, User.email == login))
    return db.scalar(statement)


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def is_admin(user: User | None) -> bool:
    return bool(user and user.platform_role == PlatformRole.ADMIN and user.is_active)


def is_teacher_account(user: User | None) -> bool:
    return bool(user and user.is_active and (user.account_role == AccountRole.TEACHER or is_admin(user)))


def login_user(request: Request, user: User) -> None:
    request.session["user_id"] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()


def push_flash(request: Request, message: str, category: str = "info") -> None:
    flashes = request.session.get("_flashes", [])
    flashes.append({"message": message, "category": category})
    request.session["_flashes"] = flashes


def pop_flashes(request: Request) -> list[dict[str, Any]]:
    flashes = request.session.pop("_flashes", [])
    return flashes if isinstance(flashes, list) else []
