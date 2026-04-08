from typing import Any

from passlib.context import CryptContext
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import User


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def find_user_by_login(db: Session, login: str) -> User | None:
    statement = select(User).where(or_(User.username == login, User.email == login))
    return db.scalar(statement)


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


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
