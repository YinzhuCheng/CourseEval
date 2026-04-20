"""User reports, target snapshots, evidence attachments, and reviewer visibility."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import is_admin, is_super_admin
from app.config import get_settings
from app.constants import (
    CourseRole,
    DiscussionGroupVisibility,
    DiscussionTopicKind,
    ReportStatus,
    ReportTargetType,
)
from app.db import utcnow
from app.models import (
    CourseMaterial,
    DiscussionGroup,
    DiscussionPost,
    FreeDiscussionTopic,
    Report,
    ReportAttachment,
    User,
)
from app.services.discussion_groups import can_view_group, get_group_member, is_group_member
from app.services.permissions import get_course_membership
from app.services.storage_paths import absolute_data_path, relative_to_data
from app.services.user_storage import record_stored_object


REPORT_REASONS = [
    ("spam", "Spam or advertising", "垃圾广告"),
    ("harassment", "Harassment or abuse", "骚扰或辱骂"),
    ("hate", "Hate or discrimination", "仇恨或歧视"),
    ("sexual", "Sexual or inappropriate content", "色情或不当内容"),
    ("violence", "Violence or dangerous content", "暴力或危险内容"),
    ("privacy", "Privacy leak", "泄露隐私"),
    ("academic", "Academic misconduct", "学术不端 / 作弊相关"),
    ("avatar", "Avatar violation", "头像违规"),
    ("other", "Other", "其他"),
]

REPORT_ATTACHMENT_LIMIT = 4
settings = get_settings()
REPORT_ATTACHMENT_MAX_BYTES = min(settings.upload_max_bytes, 10 * 1024 * 1024)


def _course_staff_for_course(db: Session, course_id: int, user_id: int) -> bool:
    member = get_course_membership(db, course_id, user_id)
    return bool(member and member.role in (CourseRole.TEACHER, CourseRole.TA))


def _report_target_context(db: Session, target_type: ReportTargetType, target_id: int, reporter: User) -> dict:
    if target_type == ReportTargetType.USER:
        target = db.get(User, target_id)
        if target is None:
            raise ValueError("target_not_found")
        return {
            "snapshot": {
                "type": target_type.value,
                "user_id": target.id,
                "username": target.username,
                "email": target.email,
                "avatar_path": target.avatar_path,
                "avatar_banned": target.avatar_banned,
            },
            "private_group_id": None,
            "course_id": None,
        }
    if target_type == ReportTargetType.TEACHING_CARD:
        material = db.get(CourseMaterial, target_id)
        if material is None:
            raise ValueError("target_not_found")
        if get_course_membership(db, material.course_id, reporter.id) is None and not is_admin(reporter):
            raise PermissionError
        return {
            "snapshot": {
                "type": target_type.value,
                "material_id": material.id,
                "course_id": material.course_id,
                "title": material.title,
                "body_markdown": material.body_markdown,
                "external_url": material.external_url,
                "created_by": material.created_by,
            },
            "private_group_id": None,
            "course_id": material.course_id,
        }
    if target_type == ReportTargetType.FREE_DISCUSSION_CARD:
        topic = db.get(FreeDiscussionTopic, target_id)
        if topic is None:
            raise ValueError("target_not_found")
        return {
            "snapshot": {
                "type": target_type.value,
                "free_discussion_topic_id": topic.id,
                "course_id": topic.course_id,
                "title": topic.title,
                "description": topic.description,
                "cover_image_path": topic.cover_image_path,
                "created_by": topic.created_by,
            },
            "private_group_id": None,
            "course_id": topic.course_id,
        }
    if target_type == ReportTargetType.DISCUSSION_GROUP_CARD:
        group = db.get(DiscussionGroup, target_id)
        if group is None:
            raise ValueError("target_not_found")
        if group.visibility == DiscussionGroupVisibility.PRIVATE and not is_group_member(db, group.id, reporter.id):
            raise PermissionError
        if not can_view_group(db, group, reporter):
            raise PermissionError
        private_id = group.id if group.visibility == DiscussionGroupVisibility.PRIVATE else None
        return {
            "snapshot": {
                "type": target_type.value,
                "discussion_group_id": group.id,
                "title": group.title,
                "description": group.description,
                "cover_image_path": group.cover_image_path,
                "visibility": group.visibility.value,
                "status": group.status.value,
                "created_by": group.created_by,
            },
            "private_group_id": private_id,
            "course_id": None,
        }
    if target_type == ReportTargetType.DISCUSSION_POST:
        post = db.scalar(
            select(DiscussionPost)
            .options(joinedload(DiscussionPost.topic))
            .where(DiscussionPost.id == target_id, DiscussionPost.deleted_at.is_(None))
        )
        if post is None or post.topic is None:
            raise ValueError("target_not_found")
        private_group_id = None
        course_id = post.topic.course_id
        if post.topic.kind == DiscussionTopicKind.DISCUSSION_GROUP:
            group = post.topic.discussion_group
            if group is None:
                raise ValueError("target_not_found")
            if group.visibility == DiscussionGroupVisibility.PRIVATE and get_group_member(db, group.id, reporter.id) is None:
                raise PermissionError
            if not can_view_group(db, group, reporter):
                raise PermissionError
            private_group_id = (
                group.id
                if group.visibility == DiscussionGroupVisibility.PRIVATE
                or post.visibility_snapshot == DiscussionGroupVisibility.PRIVATE.value
                else None
            )
            course_id = None
        elif get_course_membership(db, post.topic.course_id, reporter.id) is None and not is_admin(reporter):
            raise PermissionError
        return {
            "snapshot": {
                "type": target_type.value,
                "post_id": post.id,
                "topic_id": post.topic_id,
                "topic_kind": post.topic.kind.value,
                "author_id": post.author_id,
                "body_text": post.body_text,
                "visibility_snapshot": post.visibility_snapshot,
                "created_at": post.created_at.isoformat() if post.created_at else None,
            },
            "private_group_id": private_group_id,
            "course_id": course_id,
        }
    raise ValueError("target_not_found")


def create_report(
    db: Session,
    *,
    reporter: User,
    target_type: str,
    target_id: int,
    reason_code: str,
    reason_text: str,
    attachments: list[tuple[bytes, str, str | None]],
) -> Report:
    try:
        target = ReportTargetType(target_type)
    except ValueError:
        raise ValueError("target_not_found") from None
    valid_reason_codes = {r[0] for r in REPORT_REASONS}
    reason_code = reason_code if reason_code in valid_reason_codes else "other"
    reason_text = (reason_text or "").strip()
    if reason_code == "other" and not reason_text:
        raise ValueError("reason_required")
    if len(attachments) > REPORT_ATTACHMENT_LIMIT:
        raise ValueError("too_many_attachments")
    ctx = _report_target_context(db, target, target_id, reporter)
    report = Report(
        reporter_id=reporter.id,
        target_type=target,
        target_id=target_id,
        reason_code=reason_code,
        reason_text=reason_text or None,
        status=ReportStatus.PENDING,
        snapshot_json=json.dumps(ctx["snapshot"], ensure_ascii=False, sort_keys=True),
        private_discussion_group_id=ctx["private_group_id"],
        course_id=ctx["course_id"],
    )
    db.add(report)
    db.flush()
    for raw, filename, content_type in attachments:
        if not raw:
            continue
        if len(raw) > REPORT_ATTACHMENT_MAX_BYTES:
            raise ValueError("attachment_too_large")
        rel = store_report_attachment(report.id, raw, filename)
        att = ReportAttachment(
            report_id=report.id,
            relative_path=rel,
            original_filename=(filename or "attachment")[:255],
            content_type=(content_type or "application/octet-stream")[:255],
        )
        db.add(att)
        db.flush()
        record_stored_object(
            db,
            user_id=reporter.id,
            category="report_attachment",
            relative_path=rel,
            size_bytes=absolute_data_path(rel).stat().st_size,
            ref_type="report_attachment",
            ref_id=att.id,
        )
    return report


def store_report_attachment(report_id: int, raw: bytes, filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()[:16] or ".bin"
    upload_dir = settings.uploads_dir / "report-evidence" / f"report-{report_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{uuid4().hex}{suffix}"
    path.write_bytes(raw)
    return relative_to_data(path)


def can_view_report(db: Session, report: Report, user: User) -> bool:
    if report.reporter_id == user.id:
        return True
    if report.private_discussion_group_id is not None:
        return is_super_admin(user)
    if is_admin(user):
        return True
    if report.course_id is not None:
        return _course_staff_for_course(db, report.course_id, user.id)
    return False


def can_handle_report(db: Session, report: Report, user: User) -> bool:
    if report.reporter_id == user.id:
        return False
    if not can_view_report(db, report, user):
        return False
    if is_admin(user):
        return True
    if report.target_type == ReportTargetType.TEACHING_CARD and report.course_id is not None:
        try:
            snapshot = json.loads(report.snapshot_json or "{}")
        except json.JSONDecodeError:
            snapshot = {}
        if snapshot.get("created_by") == user.id:
            return False
        return _course_staff_for_course(db, report.course_id, user.id)
    return False


def list_reports_for_reviewer(db: Session, user: User) -> list[Report]:
    stmt = select(Report).options(joinedload(Report.reporter)).order_by(Report.created_at.desc())
    rows = list(db.scalars(stmt).unique().all())
    return [r for r in rows if can_handle_report(db, r, user)]


def list_reports_by_user(db: Session, user: User) -> list[Report]:
    return list(
        db.scalars(
            select(Report)
            .where(Report.reporter_id == user.id)
            .order_by(Report.created_at.desc())
        ).all()
    )


def update_report_status(db: Session, report: Report, *, handler: User, status: str, note: str) -> None:
    try:
        st = ReportStatus(status)
    except ValueError:
        raise ValueError("invalid_status") from None
    report.status = st
    report.handler_id = handler.id
    report.resolution_note = (note or "").strip() or None
    report.updated_at = utcnow()
    db.flush()
