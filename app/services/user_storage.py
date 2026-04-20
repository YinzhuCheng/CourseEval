"""Per-user storage quota, usage tracking, and asset purge/delete."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import is_admin, is_super_admin
from app.constants import AccountRole, PlatformRole, StorageDeletionActor
from app.db import utcnow
from app.models import (
    Assignment,
    Course,
    CourseMaterial,
    DiscussionPost,
    DiscussionPostAttachment,
    DiscussionTopic,
    FreeDiscussionTopic,
    Question,
    Submission,
    User,
    UserStoredObject,
)
from app.constants import CourseRole
from app.services.courses import is_open_community_course
from app.services.storage_paths import absolute_data_path, relative_to_data
from app.services.permissions import get_course_role

MAX_USER_STORAGE_BYTES = 5 * 1024 * 1024 * 1024 * 1024

DEFAULT_STUDENT_BYTES = 100 * 1024 * 1024
DEFAULT_TEACHER_BYTES = 1024 * 1024 * 1024

PROFILE_ASSETS_PAGE_SIZE = 20


def _policy_defaults(db: Session) -> tuple[int, int]:
    from app.models import PlatformLlmTokenPolicy

    row = db.get(PlatformLlmTokenPolicy, 1)
    if row is None:
        return DEFAULT_STUDENT_BYTES, DEFAULT_TEACHER_BYTES
    s = getattr(row, "default_student_storage_bytes", None)
    t = getattr(row, "default_teacher_storage_bytes", None)
    student_b = int(s) if s is not None and int(s) > 0 else DEFAULT_STUDENT_BYTES
    teacher_b = int(t) if t is not None and int(t) > 0 else DEFAULT_TEACHER_BYTES
    return student_b, teacher_b


def effective_storage_quota_bytes(db: Session, user: User) -> int:
    if user.storage_quota_override_bytes is not None:
        return max(0, min(int(user.storage_quota_override_bytes), MAX_USER_STORAGE_BYTES))
    student_def, teacher_def = _policy_defaults(db)
    if is_super_admin(user) or is_admin(user):
        return teacher_def
    if user.account_role == AccountRole.TEACHER:
        return teacher_def
    return student_def


def storage_summary_for_admin(db: Session, user: User) -> dict:
    return {
        "used_bytes": total_used_bytes(db, user.id),
        "quota_bytes": effective_storage_quota_bytes(db, user),
        "override_bytes": user.storage_quota_override_bytes,
    }


def total_used_bytes(db: Session, user_id: int) -> int:
    q = select(func.coalesce(func.sum(UserStoredObject.size_bytes), 0)).where(
        UserStoredObject.user_id == user_id,
        UserStoredObject.deleted_at.is_(None),
    )
    return int(db.scalar(q) or 0)


def can_add_bytes(db: Session, user_id: int, add_bytes: int) -> bool:
    if add_bytes <= 0:
        return True
    user = db.get(User, user_id)
    if user is None:
        return False
    limit = effective_storage_quota_bytes(db, user)
    return total_used_bytes(db, user_id) + add_bytes <= limit


class QuotaExceededError(Exception):
    """Raised when an upload would exceed the user's storage quota."""


def find_active_object_by_path(db: Session, relative_path: str) -> UserStoredObject | None:
    return db.scalar(
        select(UserStoredObject).where(
            UserStoredObject.relative_path == relative_path,
            UserStoredObject.deleted_at.is_(None),
        )
    )


def record_stored_object(
    db: Session,
    *,
    user_id: int,
    category: str,
    relative_path: str,
    size_bytes: int,
    ref_type: str | None = None,
    ref_id: int | None = None,
) -> UserStoredObject:
    if size_bytes <= 0:
        raise ValueError("size_bytes must be positive")
    existing = db.scalar(select(UserStoredObject).where(UserStoredObject.relative_path == relative_path))
    quota_delta = size_bytes
    if existing is not None and existing.deleted_at is None and existing.user_id == user_id:
        quota_delta = max(size_bytes - existing.size_bytes, 0)
    if not can_add_bytes(db, user_id, quota_delta):
        raise QuotaExceededError("storage_quota_exceeded")
    if existing is not None:
        existing.user_id = user_id
        existing.category = category
        existing.ref_type = ref_type
        existing.ref_id = ref_id
        existing.size_bytes = size_bytes
        existing.deleted_at = None
        existing.deleted_by_actor = None
        db.flush()
        return existing
    row = UserStoredObject(
        user_id=user_id,
        category=category,
        ref_type=ref_type,
        ref_id=ref_id,
        relative_path=relative_path,
        size_bytes=size_bytes,
    )
    db.add(row)
    db.flush()
    return row


def unlink_file_disk(relative_path: str) -> None:
    try:
        absolute_data_path(relative_path).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def soft_delete_stored_row(db: Session, row: UserStoredObject, *, actor: StorageDeletionActor, unlink: bool = True) -> None:
    if row.deleted_at is not None:
        return
    if unlink:
        unlink_file_disk(row.relative_path)
    row.deleted_at = utcnow()
    row.deleted_by_actor = actor.value
    db.flush()


def remove_avatar_storage(db: Session, user: User) -> None:
    if not user.avatar_path:
        return
    row = find_active_object_by_path(db, user.avatar_path)
    if row:
        soft_delete_stored_row(db, row, actor=StorageDeletionActor.SELF, unlink=True)
    else:
        unlink_file_disk(user.avatar_path)


def deletion_actor_for_viewer(viewer: User, *, target_user_id: int) -> StorageDeletionActor:
    if viewer.id == target_user_id:
        return StorageDeletionActor.SELF
    if is_super_admin(viewer):
        return StorageDeletionActor.SUPER_ADMIN
    if is_admin(viewer):
        return StorageDeletionActor.ADMIN
    return StorageDeletionActor.TEACHER


def can_platform_staff_purge_target(viewer: User, target: User) -> bool:
    if not is_admin(viewer):
        return False
    if is_super_admin(viewer):
        return True
    return target.platform_role != PlatformRole.SUPER_ADMIN


def submission_extra_paths(sub: Submission) -> list[str]:
    if not sub.stored_file_path:
        return []
    base = absolute_data_path(sub.stored_file_path)
    out: list[str] = []
    if base.suffix.lower() == ".pdf":
        pages_dir = base.parent / f"{base.stem}-pages"
        if pages_dir.is_dir():
            for p in sorted(pages_dir.glob("page-*.png")):
                try:
                    out.append(relative_to_data(p))
                except ValueError:
                    continue
    return out


def purge_submission_files(
    db: Session,
    sub: Submission,
    *,
    actor: StorageDeletionActor,
) -> None:
    """Remove stored file(s) for a submission; keep scores and answer_text."""
    paths = []
    if sub.stored_file_path:
        paths.append(sub.stored_file_path)
    paths.extend(submission_extra_paths(sub))
    for rel in paths:
        row = find_active_object_by_path(db, rel)
        if row:
            soft_delete_stored_row(db, row, actor=actor, unlink=True)
        else:
            unlink_file_disk(rel)
    sub.stored_file_purged_at = utcnow()
    sub.stored_file_purge_actor = actor.value
    sub.stored_file_path = None
    db.flush()
    if paths:
        try:
            first = absolute_data_path(paths[0])
            if first.suffix.lower() == ".pdf":
                pages_dir = first.parent / f"{first.stem}-pages"
                if pages_dir.is_dir():
                    shutil.rmtree(pages_dir, ignore_errors=True)
        except (OSError, ValueError):
            pass


_IMG_LINE_RE = re.compile(
    r"^\s*!\[[^\]]*\]\(\s*(/data-files/[^)]+)\s*\)\s*$",
    re.MULTILINE,
)


def _replace_image_line_with_placeholder(body: str, public_url: str, placeholder: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    for line in lines:
        m = _IMG_LINE_RE.match(line)
        if m and m.group(1).strip() == public_url.strip():
            out.append(placeholder)
        else:
            out.append(line)
    return "\n".join(out)


def public_url_from_relative(relative_path: str) -> str:
    return f"/data-files/{quote(str(relative_path), safe='/')}"


def purge_discussion_attachment(
    db: Session,
    att: DiscussionPostAttachment,
    *,
    actor: StorageDeletionActor,
    post: DiscussionPost,
) -> None:
    rel = att.relative_path
    row = find_active_object_by_path(db, rel)
    public = public_url_from_relative(rel)
    kind = Path(rel).suffix.lower().lstrip(".") or "file"
    placeholder = f"[removed:{actor.value}:{kind.upper()}]"
    post.body_text = _replace_image_line_with_placeholder(post.body_text or "", public, placeholder)
    if row:
        soft_delete_stored_row(db, row, actor=actor, unlink=True)
    else:
        unlink_file_disk(rel)
    db.delete(att)
    db.flush()


def list_user_assets_page(
    db: Session,
    user_id: int,
    *,
    page: int = 1,
    page_size: int = PROFILE_ASSETS_PAGE_SIZE,
) -> tuple[list[UserStoredObject], int, int]:
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    base = (
        select(UserStoredObject)
        .where(UserStoredObject.user_id == user_id, UserStoredObject.deleted_at.is_(None))
        .order_by(UserStoredObject.id.desc())
    )
    total = int(
        db.scalar(
            select(func.count()).select_from(UserStoredObject).where(
                UserStoredObject.user_id == user_id,
                UserStoredObject.deleted_at.is_(None),
            )
        )
        or 0
    )
    rows = list(
        db.scalars(base.offset((page - 1) * page_size).limit(page_size)).all()
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    return rows, total, total_pages


@dataclass(frozen=True)
class AssetListItem:
    object_id: int
    category: str
    label_en: str
    label_zh: str
    link_url: str | None
    size_bytes: int
    can_delete_self: bool


def describe_asset_for_profile(db: Session, obj: UserStoredObject, viewer_id: int) -> AssetListItem:
    link: str | None = None
    label_en = obj.category
    label_zh = obj.category
    can_del = viewer_id == obj.user_id

    if obj.category == "course_material_image" and obj.ref_id:
        mat = db.get(CourseMaterial, obj.ref_id)
        if mat:
            crs = db.get(Course, mat.course_id)
            title = crs.title if crs else ""
            label_en = f"Learning material image ({title})"
            label_zh = f"学习资料图片（{title}）"
            link = f"/teacher/courses/{mat.course_id}/materials/{mat.id}/edit"
    elif obj.category == "avatar":
        label_en = "Profile photo"
        label_zh = "头像"
        link = "/me/profile"
    elif obj.category == "free_discussion_cover" and obj.ref_id:
        ft = db.get(FreeDiscussionTopic, obj.ref_id)
        if ft:
            label_en = f"Open discussion topic cover ({ft.title})"
            label_zh = f"自由讨论话题封面（{ft.title}）"
            link = f"/free-discussion/topics/{ft.id}/edit"
    elif obj.category == "course_cover" and obj.ref_id:
        course = db.get(Course, obj.ref_id)
        if course:
            label_en = f"Course cover ({course.title})"
            label_zh = f"课程封面（{course.title}）"
            link = f"/teacher/courses/{course.id}"
    elif obj.category == "discussion_attachment" and obj.ref_id:
        att = db.get(DiscussionPostAttachment, obj.ref_id)
        if att:
            post = db.get(DiscussionPost, att.post_id)
            if post:
                topic = db.get(DiscussionTopic, post.topic_id)
                if topic:
                    course = db.get(Course, topic.course_id)
                    if course and is_open_community_course(course):
                        label_en = "Discussion image (open community)"
                        label_zh = "讨论区图片（自由讨论区）"
                        link = "/free-discussion"
                    elif course:
                        if topic.course_material_id:
                            label_en = f"Discussion image (course: {course.title})"
                            label_zh = f"讨论区图片（课程：{course.title}）"
                            link = f"/student/courses/{course.id}/materials/{topic.course_material_id}"
                        elif topic.question_id:
                            q = db.get(Question, topic.question_id)
                            if q:
                                asn = db.get(Assignment, q.assignment_id)
                                label_en = f"Discussion image (question discussion)"
                                label_zh = "讨论区图片（习题讨论）"
                                link = f"/student/questions/{q.id}"
    elif obj.category in ("submission", "submission_extra") and obj.ref_id:
        sub = db.get(Submission, obj.ref_id)
        if sub:
            asn = db.get(Assignment, sub.assignment_id)
            title = (asn.title if asn else "") or "assignment"
            if obj.category == "submission_extra":
                label_en = f"Submission rendered pages ({title})"
                label_zh = f"作业提交渲染页面（{title}）"
            else:
                label_en = f"Submission file ({title})"
                label_zh = f"作业提交文件（{title}）"
            link = f"/student/questions/{sub.question_id}"

    return AssetListItem(
        object_id=obj.id,
        category=obj.category,
        label_en=label_en,
        label_zh=label_zh,
        link_url=link,
        size_bytes=obj.size_bytes,
        can_delete_self=can_del,
    )


def public_url_to_relative_path(public_url: str) -> str | None:
    s = (public_url or "").strip()
    if not s.startswith("/data-files/"):
        return None
    raw = unquote(s[len("/data-files/") :].lstrip("/"))
    return raw.replace("\\", "/")


_PURGED_PLACEHOLDER_RE = re.compile(r"^\[removed:(?P<actor>[^:\]]+):(?P<kind>[^:\]]+)\]\s*$", re.IGNORECASE)


def parse_purged_placeholder_line(line: str) -> tuple[str, str] | None:
    m = _PURGED_PLACEHOLDER_RE.match((line or "").strip())
    if not m:
        return None
    return m.group("actor"), m.group("kind")


def backfill_stored_objects_from_disk(db: Session) -> None:
    """Populate user_stored_objects for existing files (idempotent)."""
    from app.config import get_settings

    from app.models import PlatformLlmTokenPolicy

    _ = db.get(PlatformLlmTokenPolicy, 1)
    settings = get_settings()

    def add_if_missing(
        user_id: int,
        category: str,
        rel: str,
        ref_type: str | None,
        ref_id: int | None,
    ) -> None:
        if not rel:
            return
        exists = db.scalar(select(UserStoredObject.id).where(UserStoredObject.relative_path == rel))
        if exists:
            return
        try:
            sz = absolute_data_path(rel).stat().st_size
        except OSError:
            return
        if sz <= 0:
            return
        db.add(
            UserStoredObject(
                user_id=user_id,
                category=category,
                ref_type=ref_type,
                ref_id=ref_id,
                relative_path=rel,
                size_bytes=sz,
            )
        )

    for user in db.scalars(select(User)).all():
        if user.avatar_path:
            add_if_missing(user.id, "avatar", user.avatar_path, "user", user.id)

    for sub in db.scalars(select(Submission).where(Submission.stored_file_path.is_not(None))).all():
        add_if_missing(sub.user_id, "submission", sub.stored_file_path, "submission", sub.id)
        for rel in submission_extra_paths(sub):
            add_if_missing(sub.user_id, "submission_extra", rel, "submission", sub.id)

    for att in db.scalars(select(DiscussionPostAttachment)).all():
        post = db.get(DiscussionPost, att.post_id)
        if post:
            add_if_missing(post.author_id, "discussion_attachment", att.relative_path, "attachment", att.id)

    cm_root = settings.uploads_dir / "course-materials"
    if cm_root.is_dir():
        for p in cm_root.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = relative_to_data(p)
            except ValueError:
                continue
            parts = rel.split("/")
            if len(parts) < 4 or parts[0] != "uploads" or parts[1] != "course-materials":
                continue
            try:
                cid = int(parts[2].split("-", 1)[1]) if parts[2].startswith("course-") else -1
                mid = int(parts[3].split("-", 1)[1]) if parts[3].startswith("material-") else -1
            except (IndexError, ValueError):
                continue
            if cid < 0 or mid < 0:
                continue
            mat = db.get(CourseMaterial, mid)
            if mat is None or mat.course_id != cid or mat.created_by is None:
                continue
            add_if_missing(mat.created_by, "course_material_image", rel, "material", mid)

    db.flush()


def viewer_may_purge_asset(db: Session, viewer: User, target_user_id: int, obj: UserStoredObject) -> bool:
    return resolve_purge_actor(db, viewer, target_user_id, obj) is not None


def resolve_purge_actor(db: Session, viewer: User, target_user_id: int, obj: UserStoredObject) -> StorageDeletionActor | None:
    """Return actor enum if viewer may purge this asset, else None."""
    if viewer.id == target_user_id:
        return StorageDeletionActor.SELF
    if is_super_admin(viewer):
        return StorageDeletionActor.SUPER_ADMIN
    target = db.get(User, target_user_id)
    if target is None:
        return None
    if is_admin(viewer):
        if not can_platform_staff_purge_target(viewer, target):
            return None
        return StorageDeletionActor.ADMIN
    if obj.category == "course_material_image" and obj.ref_id:
        mat = db.get(CourseMaterial, obj.ref_id)
        if mat is None:
            return None
        role = get_course_role(db, mat.course_id, viewer.id)
        if role == CourseRole.TEACHER:
            return StorageDeletionActor.TEACHER
        return None
    if obj.category == "avatar":
        return None
    if obj.category == "course_cover" and obj.ref_id:
        course = db.get(Course, obj.ref_id)
        if course is None:
            return None
        role = get_course_role(db, course.id, viewer.id)
        if role == CourseRole.TEACHER:
            return StorageDeletionActor.TEACHER
        return None
    if obj.category == "free_discussion_cover" and obj.ref_id:
        ft = db.get(FreeDiscussionTopic, obj.ref_id)
        if ft is None:
            return None
        course = db.get(Course, ft.course_id)
        if course is None:
            return None
        role = get_course_role(db, course.id, viewer.id)
        if role == CourseRole.TEACHER:
            return StorageDeletionActor.TEACHER
        return None
    if obj.category in ("submission", "submission_extra") and obj.ref_id:
        sub = db.get(Submission, obj.ref_id)
        if sub is None:
            return None
        role = get_course_role(db, sub.course_id, viewer.id)
        if role == CourseRole.TEACHER:
            return StorageDeletionActor.TEACHER
        return None
    if obj.category == "discussion_attachment" and obj.ref_id:
        att = db.get(DiscussionPostAttachment, obj.ref_id)
        if att is None:
            return None
        post = db.get(DiscussionPost, att.post_id)
        if post is None:
            return None
        topic = db.get(DiscussionTopic, post.topic_id)
        if topic is None:
            return None
        course = db.get(Course, topic.course_id)
        if course is None:
            return None
        role = get_course_role(db, course.id, viewer.id)
        if role == CourseRole.TEACHER:
            return StorageDeletionActor.TEACHER
        return None
    return None


def purge_submission_as_viewer(db: Session, viewer: User, submission: Submission) -> str | None:
    """Remove submission file(s) for quota; keep scores. Returns error code or None."""
    if submission.stored_file_path is None:
        return "not_found"
    actor: StorageDeletionActor | None = None
    if viewer.id == submission.user_id:
        actor = StorageDeletionActor.SELF
    elif is_super_admin(viewer):
        actor = StorageDeletionActor.SUPER_ADMIN
    elif is_admin(viewer):
        target = db.get(User, submission.user_id)
        if target is None or not can_platform_staff_purge_target(viewer, target):
            return "forbidden"
        actor = StorageDeletionActor.ADMIN
    else:
        role = get_course_role(db, submission.course_id, viewer.id)
        if role == CourseRole.TEACHER:
            actor = StorageDeletionActor.TEACHER
        else:
            return "forbidden"
    purge_submission_files(db, submission, actor=actor)
    db.commit()
    return None


def purge_user_asset(
    db: Session,
    *,
    viewer: User,
    target_user_id: int,
    object_id: int,
) -> str | None:
    """Returns None on success, or an error code string."""
    obj = db.get(UserStoredObject, object_id)
    if obj is None or obj.user_id != target_user_id or obj.deleted_at is not None:
        return "not_found"
    actor = resolve_purge_actor(db, viewer, target_user_id, obj)
    if actor is None:
        return "forbidden"
    if obj.category == "course_material_image":
        if obj.ref_id:
            mat = db.get(CourseMaterial, obj.ref_id)
            if mat is not None:
                from app.services.course_materials import remove_material_image_references

                remove_material_image_references(mat, obj.relative_path)
        soft_delete_stored_row(db, obj, actor=actor, unlink=True)
        db.flush()
        return None
    if obj.category == "course_cover" and obj.ref_id:
        course = db.get(Course, obj.ref_id)
        if course is None:
            return "not_found"
        if course.cover_image_path == obj.relative_path:
            course.cover_image_path = None
            course.updated_at = utcnow()
        soft_delete_stored_row(db, obj, actor=actor, unlink=True)
        db.flush()
        return None
    if obj.category == "free_discussion_cover" and obj.ref_id:
        ft = db.get(FreeDiscussionTopic, obj.ref_id)
        if ft is None:
            return "not_found"
        if ft.cover_image_path == obj.relative_path:
            ft.cover_image_path = None
            ft.updated_at = utcnow()
        soft_delete_stored_row(db, obj, actor=actor, unlink=True)
        db.flush()
        return None
    if obj.category == "avatar":
        user = db.get(User, target_user_id)
        if user is None:
            return "not_found"
        remove_avatar_storage(db, user)
        user.avatar_path = None
        user.updated_at = utcnow()
        db.flush()
        return None
    if obj.category in ("submission", "submission_extra") and obj.ref_id:
        sub = db.get(Submission, obj.ref_id)
        if sub is None:
            return "not_found"
        if obj.category == "submission":
            purge_submission_files(db, sub, actor=actor)
        else:
            row = find_active_object_by_path(db, obj.relative_path)
            if row:
                soft_delete_stored_row(db, row, actor=actor, unlink=True)
            else:
                unlink_file_disk(obj.relative_path)
        db.flush()
        return None
    if obj.category == "discussion_attachment" and obj.ref_id:
        att = db.get(DiscussionPostAttachment, obj.ref_id)
        if att is None:
            return "not_found"
        post = db.get(DiscussionPost, att.post_id)
        if post is None:
            return "not_found"
        purge_discussion_attachment(db, att, actor=actor, post=post)
        db.flush()
        return None
    return "unsupported"
