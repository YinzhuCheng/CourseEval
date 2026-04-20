"""Course learning materials (teacher-managed)."""

from __future__ import annotations

import re
from urllib.parse import quote, urlparse
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import utcnow
from app.models import Course, CourseMaterial, User
from app.services.discussions import get_or_create_material_topic
from app.services.image_uploads import normalize_uploaded_image
from app.services.storage_paths import relative_to_data

settings = get_settings()


def normalize_external_url(external_url: str | None) -> str | None:
    url = (external_url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid_external_url")
    return url


def list_materials_for_course(db: Session, course_id: int) -> list[CourseMaterial]:
    return list(
        db.scalars(
            select(CourseMaterial)
            .where(CourseMaterial.course_id == course_id)
            .order_by(CourseMaterial.sort_order.asc(), CourseMaterial.id.asc())
        ).all()
    )


def list_materials_for_free_topic(db: Session, course_id: int, free_topic_id: int) -> list[CourseMaterial]:
    rows = list(
        db.scalars(
            select(CourseMaterial)
            .where(CourseMaterial.course_id == course_id, CourseMaterial.free_discussion_topic_id == free_topic_id)
            .order_by(CourseMaterial.sort_order.asc(), CourseMaterial.id.asc())
        ).all()
    )
    if rows:
        return rows
    return [
        item
        for item in list_materials_for_course(db, course_id)
        if item.free_discussion_topic_id is None and (item.sort_order or 0) // 100000 == free_topic_id
    ]


def get_material_for_course(db: Session, material_id: int, course_id: int) -> CourseMaterial | None:
    return db.scalar(
        select(CourseMaterial)
        .options(joinedload(CourseMaterial.course))
        .where(CourseMaterial.id == material_id, CourseMaterial.course_id == course_id)
    )


def create_material(
    db: Session,
    *,
    course: Course,
    title: str,
    body_markdown: str | None,
    external_url: str | None,
    creator: User,
    free_discussion_topic_id: int | None = None,
) -> CourseMaterial:
    title = title.strip()
    if not title:
        raise ValueError("title_required")
    m = CourseMaterial(
        course_id=course.id,
        title=title,
        body_markdown=(body_markdown or "").strip() or None,
        external_url=normalize_external_url(external_url),
        sort_order=0,
        free_discussion_topic_id=free_discussion_topic_id,
        created_by=creator.id,
        updated_at=utcnow(),
    )
    db.add(m)
    db.flush()
    get_or_create_material_topic(db, m.id, course.id)
    return m


def update_material(
    db: Session,
    material: CourseMaterial,
    *,
    title: str,
    body_markdown: str | None,
    external_url: str | None,
) -> CourseMaterial:
    material.title = title.strip()
    if not material.title:
        raise ValueError("title_required")
    material.body_markdown = (body_markdown or "").strip() or None
    material.external_url = normalize_external_url(external_url)
    material.updated_at = utcnow()
    return material


def store_material_image(course_id: int, material_id: int, file_bytes: bytes, original_filename: str) -> str:
    ext, cleaned = normalize_uploaded_image(file_bytes, original_filename)
    upload_dir = settings.uploads_dir / "course-materials" / f"course-{course_id}" / f"material-{material_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / f"{uuid4().hex}{ext}"
    stored.write_bytes(cleaned)
    return relative_to_data(stored)


def remove_material_image_references(material: CourseMaterial, relative_path: str) -> None:
    if not material.body_markdown:
        return
    public_url = f"/data-files/{quote(relative_path, safe='/')}"
    escaped_url = re.escape(public_url)
    body = material.body_markdown
    body = re.sub(rf"!\[[^\]]*\]\(\s*{escaped_url}\s*\)", "", body)
    body = re.sub(rf"\[[^\]]*\]\(\s*{escaped_url}\s*\)", "", body)
    body = body.replace(public_url, "")
    material.body_markdown = re.sub(r"\n{3,}", "\n\n", body).strip() or None
    material.updated_at = utcnow()
