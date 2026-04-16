import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import get_settings
from app.constants import AccountRole, PlatformRole, UserRole
from app.db import utcnow
from app.models import User


SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_LEN = 64
EMAIL_VERIFICATION_TOKEN_BYTES = 32


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


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active or not user.email_verified:
        request.session.pop("user_id", None)
        return None
    return user


def is_super_admin(user: User | None) -> bool:
    return bool(user and user.platform_role == PlatformRole.SUPER_ADMIN and user.is_active and user.email_verified)


def is_admin(user: User | None) -> bool:
    return bool(
        user
        and user.platform_role in {PlatformRole.ADMIN, PlatformRole.SUPER_ADMIN}
        and user.is_active
        and user.email_verified
    )


def is_teacher_account(user: User | None) -> bool:
    return bool(user and user.is_active and user.email_verified and (user.account_role == AccountRole.TEACHER or is_admin(user)))


def has_super_admin(db: Session, *, require_verified: bool = False) -> bool:
    statement = select(User.id).where(
        User.platform_role == PlatformRole.SUPER_ADMIN,
        User.is_active.is_(True),
    )
    if require_verified:
        statement = statement.where(User.email_verified.is_(True))
    return db.scalar(statement) is not None


def resolve_registration_roles(db: Session) -> tuple[AccountRole, PlatformRole]:
    # Bootstrap logic: the very first successful registration becomes the only
    # automatically created super administrator for the system lifetime.
    if not has_super_admin(db):
        return AccountRole.STUDENT, PlatformRole.SUPER_ADMIN
    return AccountRole.STUDENT, PlatformRole.USER


def generate_email_verification_token() -> str:
    return secrets.token_urlsafe(EMAIL_VERIFICATION_TOKEN_BYTES)


def invite_registration_enabled() -> bool:
    return bool(get_settings().registration_invite_code)


def valid_registration_invite_code(invite_code: str) -> bool:
    configured_code = get_settings().registration_invite_code
    normalized_invite_code = invite_code.strip()
    return bool(
        configured_code
        and normalized_invite_code
        and hmac.compare_digest(normalized_invite_code, configured_code)
    )


def generate_internal_email_address() -> str:
    domain = get_settings().internal_email_domain.strip() or "invite.local"
    return f"invite-{secrets.token_hex(12)}@{domain}"


def initial_email_verification_state() -> dict[str, object]:
    return {
        "email_verified": False,
        "email_verification_token": generate_email_verification_token(),
        "email_verification_sent_at": utcnow(),
    }


def refresh_email_verification(user: User) -> None:
    user.email_verified = False
    user.email_verification_token = generate_email_verification_token()
    user.email_verification_sent_at = utcnow()


def can_verify_email_token(user: User) -> bool:
    sent_at = _as_utc(user.email_verification_sent_at)
    token = user.email_verification_token
    if not token or sent_at is None:
        return False
    expires_after = timedelta(hours=get_settings().email_verification_expire_hours)
    return utcnow() <= sent_at + expires_after


def mark_email_verified(user: User) -> None:
    user.email_verified = True
    user.email_verification_token = None
    user.email_verification_sent_at = None


def assign_user_role(user: User, role: UserRole) -> None:
    if role == UserRole.SUPER_ADMIN:
        user.account_role = AccountRole.STUDENT
        user.platform_role = PlatformRole.SUPER_ADMIN
    elif role == UserRole.ADMIN:
        user.account_role = AccountRole.STUDENT
        user.platform_role = PlatformRole.ADMIN
    elif role == UserRole.TEACHER:
        user.account_role = AccountRole.TEACHER
        user.platform_role = PlatformRole.USER
    else:
        user.account_role = AccountRole.STUDENT
        user.platform_role = PlatformRole.USER


def landing_path_for_user(user: User | None) -> str:
    if user is None or not user.is_active or not user.email_verified:
        return "/login"
    if user.effective_role in {UserRole.ADMIN, UserRole.SUPER_ADMIN}:
        return "/admin/system"
    if user.effective_role == UserRole.TEACHER:
        return "/teacher/courses"
    return "/student/courses"


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
