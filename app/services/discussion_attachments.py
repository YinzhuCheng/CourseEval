"""Store discussion post images under data/uploads/discussion-images/course-{id}/post-{id}/."""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from PIL import Image
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.storage_paths import relative_to_data

if TYPE_CHECKING:
    from app.models import DiscussionPost

settings = get_settings()

DISCUSSION_MAX_IMAGES_PER_POST = 4

_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def discussion_image_public_path(relative_path: str) -> str:
    from urllib.parse import quote

    return f"/data-files/{quote(relative_path, safe='/')}"


def _strip_image_metadata(file_bytes: bytes, ext: str) -> bytes:
    """Remove EXIF and most metadata where Pillow supports it; on failure return original."""
    try:
        im = Image.open(io.BytesIO(file_bytes))
        im.load()
    except Exception:
        return file_bytes
    fmt = (im.format or "").upper()
    out = io.BytesIO()
    try:
        if fmt == "JPEG":
            rgb = im.convert("RGB")
            rgb.save(out, format="JPEG", quality=88, optimize=True)
        elif fmt == "PNG":
            im.save(out, format="PNG", optimize=True)
        elif fmt == "WEBP":
            im.save(out, format="WEBP", quality=85, method=6)
        elif fmt == "GIF":
            im.save(out, format="GIF", save_all=True)
        else:
            return file_bytes
    except Exception:
        return file_bytes
    return out.getvalue()


def store_discussion_post_image(course_id: int, post_id: int, file_bytes: bytes, original_filename: str) -> str:
    ext = Path(original_filename).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise ValueError("unsupported_image_type")
    if len(file_bytes) > settings.upload_max_bytes:
        raise ValueError("file_too_large")
    cleaned = _strip_image_metadata(file_bytes, ext)
    upload_dir = settings.uploads_dir / "discussion-images" / f"course-{course_id}" / f"post-{post_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / f"{uuid4().hex}{ext}"
    stored.write_bytes(cleaned)
    return relative_to_data(stored)


def attach_discussion_images_to_post(
    db: Session,
    post: "DiscussionPost",
    course_id: int,
    files: list[tuple[bytes, str]],
) -> None:
    """Persist images and append markdown to post body. Raises ValueError on limits."""
    from app.models import DiscussionPostAttachment

    if not files:
        return
    existing = len(post.attachments or [])
    if existing + len(files) > DISCUSSION_MAX_IMAGES_PER_POST:
        raise ValueError("too_many_images")
    extra_lines: list[str] = []
    for raw, name in files:
        rel = store_discussion_post_image(course_id, post.id, raw, name)
        db.add(DiscussionPostAttachment(post_id=post.id, relative_path=rel))
        extra_lines.append(f"![]({discussion_image_public_path(rel)})")
    if extra_lines:
        base = (post.body_text or "").rstrip()
        post.body_text = base + ("\n\n" if base else "") + "\n\n".join(extra_lines)


def delete_attachment_file(relative_path: str) -> None:
    from app.services.storage_paths import absolute_data_path

    try:
        p = absolute_data_path(relative_path)
    except ValueError:
        return
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
