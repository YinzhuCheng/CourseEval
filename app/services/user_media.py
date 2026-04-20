"""User avatar uploads (stored under data/uploads)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4


from app.config import get_settings
from app.models import User
from app.services.image_uploads import normalize_uploaded_image

settings = get_settings()


def _relative_upload(path: Path) -> str:
    return path.relative_to(settings.data_dir).as_posix()


def store_user_avatar(user_id: int, file_bytes: bytes, original_filename: str) -> str:
    ext, cleaned = normalize_uploaded_image(file_bytes, original_filename)
    upload_dir = settings.uploads_dir / "avatars" / f"user-{user_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / f"{uuid4().hex}{ext}"
    stored.write_bytes(cleaned)
    return _relative_upload(stored)


def store_course_cover_image(course_id: int, file_bytes: bytes, original_filename: str) -> str:
    ext, cleaned = normalize_uploaded_image(file_bytes, original_filename)
    upload_dir = settings.uploads_dir / "courses" / f"course-{course_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    for old in upload_dir.glob("cover.*"):
        try:
            old.unlink()
        except OSError:
            pass
    stored = upload_dir / f"cover{ext}"
    stored.write_bytes(cleaned)
    return _relative_upload(stored)


def clear_course_cover_files(course_id: int) -> None:
    upload_dir = settings.uploads_dir / "courses" / f"course-{course_id}"
    if not upload_dir.is_dir():
        return
    for old in upload_dir.glob("cover.*"):
        try:
            old.unlink()
        except OSError:
            pass


def can_view_user_avatar_path(viewer_id: int | None, owner_id: int) -> bool:
    """Any logged-in user may view others' avatars (for discussions)."""
    return viewer_id is not None


def user_avatar_public_url(user: User | None) -> str | None:
    if user is None or getattr(user, "avatar_banned", False):
        return None
    path = getattr(user, "avatar_path", None)
    if not path:
        return None
    from urllib.parse import quote

    return f"/data-files/{quote(str(path), safe='/')}"
