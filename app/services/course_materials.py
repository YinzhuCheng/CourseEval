"""Course learning materials (teacher-managed)."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import utcnow
from app.models import Course, CourseMaterial, User
from app.services.discussions import get_or_create_material_topic
from app.services.storage_paths import absolute_data_path, relative_to_data

settings = get_settings()


def list_materials_for_course(db: Session, course_id: int) -> list[CourseMaterial]:
    return list(
        db.scalars(
            select(CourseMaterial)
            .where(CourseMaterial.course_id == course_id)
            .order_by(CourseMaterial.sort_order.asc(), CourseMaterial.id.asc())
        ).all()
    )


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
) -> CourseMaterial:
    title = title.strip()
    if not title:
        raise ValueError("title_required")
    m = CourseMaterial(
        course_id=course.id,
        title=title,
        body_markdown=(body_markdown or "").strip() or None,
        external_url=(external_url or "").strip() or None,
        sort_order=0,
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
    material.external_url = (external_url or "").strip() or None
    material.updated_at = utcnow()
    return material


def store_material_image(course_id: int, material_id: int, file_bytes: bytes, original_filename: str) -> str:
    ext = Path(original_filename).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        raise ValueError("unsupported_image_type")
    if len(file_bytes) > settings.upload_max_bytes:
        raise ValueError("file_too_large")
    upload_dir = settings.uploads_dir / "course-materials" / f"course-{course_id}" / f"material-{material_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / f"{uuid4().hex}{ext}"
    stored.write_bytes(file_bytes)
    return relative_to_data(stored)
