"""Serve user-uploaded files from data/ with access control."""

from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import get_settings
from app.constants import MembershipStatus
from app.db import get_db
from app.models import CourseMember
from app.services.courses import get_course_for_staff
from app.services.permissions import RedirectRequired, require_user
from app.services.storage_paths import absolute_data_path
from app.services.user_media import can_view_user_avatar_path

router = APIRouter(tags=["uploads"])


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _active_course_member(db: Session, course_id: int, user_id: int) -> bool:
    return db.scalar(
        select(CourseMember.id).where(
            CourseMember.course_id == course_id,
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    ) is not None


@router.get("/data-files/{relative_path:path}")
def serve_data_file(relative_path: str, request: Request, db: Session = Depends(get_db)):
    try:
        viewer = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)

    raw = unquote(relative_path).lstrip("/")
    try:
        path = absolute_data_path(raw)
        path = path.resolve()
        base = get_settings().data_dir.resolve()
        if base not in path.parents and path != base:
            return _redirect("/student/courses")
    except (OSError, ValueError):
        return _redirect("/student/courses")

    if not path.exists() or not path.is_file():
        return _redirect("/student/courses")

    rel_parts = Path(raw).parts

    # course-materials/course-{id}/...
    if len(rel_parts) >= 2 and rel_parts[0] == "uploads" and rel_parts[1] == "course-materials":
        try:
            course_id = int(rel_parts[2].split("-", 1)[1]) if rel_parts[2].startswith("course-") else -1
        except (IndexError, ValueError):
            course_id = -1
        if course_id < 0 or (
            not _active_course_member(db, course_id, viewer.id) and get_course_for_staff(db, course_id, viewer.id) is None
        ):
            return _redirect("/student/courses")
    # avatars/user-{id}/...
    elif len(rel_parts) >= 3 and rel_parts[0] == "uploads" and rel_parts[1] == "avatars" and rel_parts[2].startswith("user-"):
        try:
            owner_id = int(rel_parts[2].split("-", 1)[1])
        except (IndexError, ValueError):
            return _redirect("/student/courses")
        if not can_view_user_avatar_path(viewer.id, owner_id):
            return _redirect("/student/courses")
    # courses/course-{id}/cover.*
    elif len(rel_parts) >= 3 and rel_parts[0] == "uploads" and rel_parts[1] == "courses" and rel_parts[2].startswith("course-"):
        try:
            course_id = int(rel_parts[2].split("-", 1)[1])
        except (IndexError, ValueError):
            return _redirect("/student/courses")
        if not _active_course_member(db, course_id, viewer.id) and get_course_for_staff(db, course_id, viewer.id) is None:
            return _redirect("/student/courses")
    # free-discussion/course-{id}/topic-{id}/...
    elif (
        len(rel_parts) >= 4
        and rel_parts[0] == "uploads"
        and rel_parts[1] == "free-discussion"
        and rel_parts[2].startswith("course-")
        and rel_parts[3].startswith("topic-")
    ):
        try:
            course_id = int(rel_parts[2].split("-", 1)[1])
        except (IndexError, ValueError):
            return _redirect("/student/courses")
        if not _active_course_member(db, course_id, viewer.id) and get_course_for_staff(db, course_id, viewer.id) is None:
            return _redirect("/student/courses")
    # discussion-images/course-{id}/post-{id}/...
    elif (
        len(rel_parts) >= 4
        and rel_parts[0] == "uploads"
        and rel_parts[1] == "discussion-images"
        and rel_parts[2].startswith("course-")
        and rel_parts[3].startswith("post-")
    ):
        try:
            course_id = int(rel_parts[2].split("-", 1)[1])
        except (IndexError, ValueError):
            return _redirect("/student/courses")
        if not _active_course_member(db, course_id, viewer.id) and get_course_for_staff(db, course_id, viewer.id) is None:
            return _redirect("/student/courses")
    else:
        return _redirect("/student/courses")

    suffix = path.suffix.lower()
    media = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    return FileResponse(path=path, media_type=media)
