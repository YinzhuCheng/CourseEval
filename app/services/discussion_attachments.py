"""Store discussion post images under data/uploads/discussion-images/course-{id}/post-{id}/."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.image_uploads import normalize_uploaded_image
from app.services.storage_paths import absolute_data_path, relative_to_data

if TYPE_CHECKING:
    from app.models import DiscussionPost

settings = get_settings()

DISCUSSION_MAX_IMAGES_PER_POST = 4

def discussion_image_public_path(relative_path: str) -> str:
    from urllib.parse import quote

    return f"/data-files/{quote(relative_path, safe='/')}"


def store_discussion_post_image(course_id: int, post_id: int, file_bytes: bytes, original_filename: str) -> str:
    ext, cleaned = normalize_uploaded_image(file_bytes, original_filename)
    upload_dir = settings.uploads_dir / "discussion-images" / f"course-{course_id}" / f"post-{post_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / f"{uuid4().hex}{ext}"
    stored.write_bytes(cleaned)
    return relative_to_data(stored)


def delete_discussion_attachment_files(relative_paths: list[str]) -> None:
    for relative_path in relative_paths:
        delete_attachment_file(relative_path)


def attach_discussion_images_to_post(
    db: Session,
    post: "DiscussionPost",
    course_id: int,
    files: list[tuple[bytes, str]],
) -> list[str]:
    """Persist images and append markdown to post body. Raises ValueError on limits."""
    from app.models import DiscussionPostAttachment

    if not files:
        return []
    existing = len(post.attachments or [])
    if existing + len(files) > DISCUSSION_MAX_IMAGES_PER_POST:
        raise ValueError("too_many_images")
    extra_lines: list[str] = []
    stored_paths: list[str] = []
    try:
        from app.services.user_storage import QuotaExceededError, record_stored_object

        for raw, name in files:
            rel = store_discussion_post_image(course_id, post.id, raw, name)
            stored_paths.append(rel)
            att = DiscussionPostAttachment(post_id=post.id, relative_path=rel)
            db.add(att)
            db.flush()
            try:
                sz = absolute_data_path(rel).stat().st_size
                record_stored_object(
                    db,
                    user_id=post.author_id,
                    category="discussion_attachment",
                    relative_path=rel,
                    size_bytes=sz,
                    ref_type="attachment",
                    ref_id=att.id,
                )
            except QuotaExceededError:
                db.delete(att)
                delete_attachment_file(rel)
                raise ValueError("storage_quota_exceeded")
            extra_lines.append(f"![]({discussion_image_public_path(rel)})")
        if extra_lines:
            base = (post.body_text or "").rstrip()
            post.body_text = base + ("\n\n" if base else "") + "\n\n".join(extra_lines)
    except Exception:
        delete_discussion_attachment_files(stored_paths)
        raise
    return stored_paths


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
