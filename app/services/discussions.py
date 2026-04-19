"""Discussion topics, posts, visibility rules, pagination helpers, and moderation."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.auth import is_admin, is_super_admin
from app.constants import AccountRole, AssignmentStatus, CourseRole, DiscussionTopicKind, MembershipStatus
from app.db import utcnow
from app.models import (
    Assignment,
    CourseDiscussionMute,
    CourseMember,
    DiscussionModerationLog,
    DiscussionPost,
    DiscussionPostAttachment,
    DiscussionTopic,
    PlatformLlmTokenPolicy,
    Question,
    User,
)
from app.services.discussion_attachments import DISCUSSION_MAX_IMAGES_PER_POST
from app.services.discussion_markdown import (
    DISCUSSION_BODY_MAX_CHARS,
    has_disallowed_remote_image_markdown,
)
from app.services.image_uploads import ALLOWED_IMAGE_EXTENSIONS, human_upload_max_bytes


def course_member_roles_map(db: Session, course_id: int) -> dict[int, CourseRole]:
    rows = db.execute(
        select(CourseMember.user_id, CourseMember.role).where(
            CourseMember.course_id == course_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    ).all()
    return {int(r[0]): r[1] for r in rows}


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def assignment_past_close_for_discussion(assignment: Assignment) -> bool:
    """Question discussion and post-close reveal use assignment.close_at (when set)."""
    close_at = _as_utc(assignment.close_at)
    if close_at is None:
        return False
    return utcnow() >= close_at


def get_discussion_posts_page_size(db: Session) -> int:
    row = db.get(PlatformLlmTokenPolicy, 1)
    if row is None:
        return 50
    v = int(row.discussion_posts_page_size)
    return max(10, min(200, v))


def get_or_create_material_topic(db: Session, material_id: int, course_id: int) -> DiscussionTopic:
    existing = db.scalar(
        select(DiscussionTopic).where(
            DiscussionTopic.course_material_id == material_id,
            DiscussionTopic.kind == DiscussionTopicKind.COURSE_MATERIAL,
        )
    )
    if existing:
        return existing
    topic = DiscussionTopic(
        course_id=course_id,
        kind=DiscussionTopicKind.COURSE_MATERIAL,
        course_material_id=material_id,
        question_id=None,
    )
    db.add(topic)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(DiscussionTopic).where(
                DiscussionTopic.course_material_id == material_id,
                DiscussionTopic.kind == DiscussionTopicKind.COURSE_MATERIAL,
            )
        )
        if existing:
            return existing
        raise
    return topic


def get_or_create_question_topic(db: Session, question_id: int, course_id: int) -> DiscussionTopic:
    existing = db.scalar(
        select(DiscussionTopic).where(
            DiscussionTopic.question_id == question_id,
            DiscussionTopic.kind == DiscussionTopicKind.QUESTION,
        )
    )
    if existing:
        return existing
    topic = DiscussionTopic(
        course_id=course_id,
        kind=DiscussionTopicKind.QUESTION,
        course_material_id=None,
        question_id=question_id,
    )
    db.add(topic)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(DiscussionTopic).where(
                DiscussionTopic.question_id == question_id,
                DiscussionTopic.kind == DiscussionTopicKind.QUESTION,
            )
        )
        if existing:
            return existing
        raise
    return topic


def viewer_course_role(db: Session, course_id: int, user_id: int) -> CourseRole | None:
    m = db.scalar(
        select(CourseMember).where(
            CourseMember.course_id == course_id,
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return m.role if m else None


def is_user_muted_for_course(db: Session, course_id: int, user_id: int) -> bool:
    row = db.scalar(
        select(CourseDiscussionMute).where(
            CourseDiscussionMute.course_id == course_id,
            CourseDiscussionMute.user_id == user_id,
        )
    )
    if row is None:
        return False
    until = _as_utc(row.muted_until)
    if until is None:
        return True
    return until > utcnow()


def can_moderate_discussion(db: Session, course_id: int, user: User) -> bool:
    if is_super_admin(user) or is_admin(user):
        return True
    role = viewer_course_role(db, course_id, user.id)
    return role in (CourseRole.TEACHER, CourseRole.TA)


def can_post_on_material_topic(db: Session, course_id: int, user: User) -> bool:
    role = viewer_course_role(db, course_id, user.id)
    if role not in (CourseRole.STUDENT, CourseRole.TEACHER, CourseRole.TA):
        return False
    if is_user_muted_for_course(db, course_id, user.id):
        return False
    return True


def can_post_on_question_topic(db: Session, question: Question, user: User) -> bool:
    role = viewer_course_role(db, question.assignment.course_id, user.id)
    if role not in (CourseRole.STUDENT, CourseRole.TEACHER, CourseRole.TA):
        return False
    if is_user_muted_for_course(db, question.assignment.course_id, user.id):
        return False
    asn = question.assignment
    if asn.status == AssignmentStatus.DRAFT and role == CourseRole.STUDENT:
        return False
    if role in (CourseRole.TEACHER, CourseRole.TA):
        return True
    if not assignment_past_close_for_discussion(asn):
        return False
    return True


def count_posts_for_topic(db: Session, topic_id: int) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(DiscussionPost)
            .where(DiscussionPost.topic_id == topic_id, DiscussionPost.deleted_at.is_(None))
        )
        or 0
    )


def get_root_post_for_topic(db: Session, topic_id: int) -> DiscussionPost | None:
    """Earliest top-level post in the topic (the 'first floor' / main thread opener)."""
    return db.scalar(
        select(DiscussionPost)
        .where(
            DiscussionPost.topic_id == topic_id,
            DiscussionPost.parent_post_id.is_(None),
            DiscussionPost.deleted_at.is_(None),
        )
        .order_by(DiscussionPost.created_at.asc())
        .limit(1)
    )


def list_posts_for_topic(
    db: Session, topic_id: int, *, offset: int = 0, limit: int | None = None
) -> list[DiscussionPost]:
    stmt = (
        select(DiscussionPost)
        .options(joinedload(DiscussionPost.author))
        .where(DiscussionPost.topic_id == topic_id, DiscussionPost.deleted_at.is_(None))
        .order_by(DiscussionPost.created_at.asc())
    )
    if limit is not None:
        stmt = stmt.offset(max(0, offset)).limit(limit)
    return list(db.scalars(stmt).all())


def attach_avatar_and_role_badges(
    db: Session,
    course_id: int,
    flat_rows: list[dict],
    viewer: User | None = None,
    *,
    topic_owner_user_id: int | None = None,
) -> list[dict]:
    """Add avatar_url, role_badges, body_html for template."""
    from app.services.discussion_markdown import render_discussion_markdown
    from app.services.user_media import user_avatar_public_url

    roles = course_member_roles_map(db, course_id)
    for r in flat_rows:
        u = r["post"].author
        is_ai = getattr(r["post"], "is_ai", False)
        if is_ai:
            r["avatar_url"] = None
            r["role_badges"] = ["ai_assistant"]
        else:
            r["avatar_url"] = None if r["post"].is_anonymous else user_avatar_public_url(u)
            badges: list[str] = []
            if is_super_admin(u):
                badges.append("super_admin")
            elif is_admin(u):
                badges.append("admin")
            if topic_owner_user_id is not None and u.id == topic_owner_user_id:
                badges.append("free_topic_owner")
            cr = roles.get(u.id)
            if cr == CourseRole.TEACHER:
                badges.append("course_teacher")
            elif cr == CourseRole.TA:
                badges.append("course_ta")
            elif u.account_role == AccountRole.TEACHER and not badges:
                badges.append("account_teacher")
            r["role_badges"] = badges
        r["body_html"] = render_discussion_markdown(
            r["post"].body_text,
            for_ai=is_ai,
            viewer_user_id=viewer.id if viewer else None,
            post_author_id=r["post"].author_id,
        )
    return flat_rows


def flat_thread_for_template(posts: list[DiscussionPost], decorated: list[dict]) -> list[dict]:
    """Ordered flat list with reply depth for simple template rendering."""
    by_id = {d["post"].id: d for d in decorated}

    def depth_of(post_id: int, seen: set[int] | None = None) -> int:
        seen = set() if seen is None else seen
        if post_id in seen:
            return 0
        seen.add(post_id)
        p = by_id.get(post_id)
        if not p:
            return 0
        parent_id = p["post"].parent_post_id
        if not parent_id:
            return 0
        return 1 + depth_of(parent_id, seen)

    out: list[dict] = []
    for d in decorated:
        out.append({**d, "depth": depth_of(d["post"].id)})
    return out


def display_label_for_post(post: DiscussionPost, viewer: User | None, db: Session, course_id: int) -> tuple[str, str | None]:
    """Return (primary label, secondary hint for staff)."""
    if viewer is None:
        return ("", None)
    if getattr(post, "is_ai", False):
        return ("AI", None)
    if not post.is_anonymous:
        return (post.author.username, None)

    if is_super_admin(viewer) or is_admin(viewer):
        return (post.author.username, "admin")

    role = viewer_course_role(db, course_id, viewer.id)
    if role == CourseRole.STUDENT:
        return ("匿名", None)

    if post.author.account_role != AccountRole.TEACHER:
        return (post.author.username, "staff_student_anon")
    if post.author_id == viewer.id:
        return (post.author.username, "self_anon")
    return ("匿名教师", None)


def _log_moderation(
    db: Session,
    *,
    course_id: int,
    actor: User | None,
    action: str,
    target_user_id: int | None = None,
    post_id: int | None = None,
    detail: str | None = None,
) -> None:
    db.add(
        DiscussionModerationLog(
            course_id=course_id,
            actor_id=actor.id if actor else None,
            action=action,
            target_user_id=target_user_id,
            post_id=post_id,
            detail=detail,
        )
    )


def mute_user_in_course(
    db: Session,
    *,
    course_id: int,
    target_user_id: int,
    actor: User,
    muted_until: datetime | None,
) -> None:
    row = db.scalar(
        select(CourseDiscussionMute).where(
            CourseDiscussionMute.course_id == course_id,
            CourseDiscussionMute.user_id == target_user_id,
        )
    )
    if row is None:
        row = CourseDiscussionMute(
            course_id=course_id,
            user_id=target_user_id,
            muted_until=muted_until,
            created_by_id=actor.id,
        )
        db.add(row)
    else:
        row.muted_until = muted_until
        row.created_by_id = actor.id
    db.flush()
    _log_moderation(
        db,
        course_id=course_id,
        actor=actor,
        action="mute",
        target_user_id=target_user_id,
        detail=None if muted_until is None else muted_until.isoformat(),
    )


def unmute_user_in_course(db: Session, *, course_id: int, target_user_id: int, actor: User) -> None:
    row = db.scalar(
        select(CourseDiscussionMute).where(
            CourseDiscussionMute.course_id == course_id,
            CourseDiscussionMute.user_id == target_user_id,
        )
    )
    if row:
        db.delete(row)
    _log_moderation(db, course_id=course_id, actor=actor, action="unmute", target_user_id=target_user_id)


def hard_delete_post(db: Session, post: DiscussionPost, *, actor: User, course_id: int) -> None:
    from app.constants import StorageDeletionActor
    from app.services.discussion_attachments import delete_attachment_file
    from app.services.user_storage import find_active_object_by_path, soft_delete_stored_row

    for att in list(post.attachments or []):
        row = find_active_object_by_path(db, att.relative_path)
        if row is not None:
            soft_delete_stored_row(db, row, actor=StorageDeletionActor.TEACHER, unlink=True)
        else:
            delete_attachment_file(att.relative_path)
        db.delete(att)
    _log_moderation(db, course_id=course_id, actor=actor, action="delete_post", post_id=post.id)
    db.delete(post)


def can_delete_discussion_post(db: Session, user: User, post: DiscussionPost, course_id: int) -> bool:
    if getattr(post, "deleted_at", None) is not None:
        return False
    return can_moderate_discussion(db, course_id, user)


def can_moderate_free_topic_as_owner(
    db: Session, course_id: int, user: User, topic_owner_user_id: int | None
) -> bool:
    """Admins, course staff, or the user who created the free-discussion topic card."""
    if can_moderate_discussion(db, course_id, user):
        return True
    if topic_owner_user_id is not None and topic_owner_user_id == user.id:
        return True
    return False


def build_discussion_view_context(
    db: Session,
    *,
    topic_id: int,
    course_id: int,
    viewer: User,
    request: object,
    topic_owner_user_id: int | None = None,
) -> dict:
    """Thread rows, pagination dict, moderation flag for discussion partial."""
    from app.services.discussion_ai import discussion_ai_group_options

    page_size = get_discussion_posts_page_size(db)
    page, anchor = parse_discussion_query(request)
    pag = discussion_pagination_state(
        db,
        topic_id=topic_id,
        page_size=page_size,
        page=page,
        anchor_post_id=anchor,
    )
    posts = list_posts_for_topic(
        db, topic_id, offset=pag["offset"], limit=pag["page_size"]
    )
    decorated = []
    for p in posts:
        label, hint = display_label_for_post(p, viewer, db, course_id)
        decorated.append(
            {
                "post": p,
                "display_name": label,
                "staff_hint": hint,
                "can_delete": can_delete_discussion_post(db, viewer, p, course_id)
                or (topic_owner_user_id is not None and viewer.id == topic_owner_user_id),
            }
        )
    threaded = attach_avatar_and_role_badges(
        db,
        course_id,
        flat_thread_for_template(posts, decorated),
        viewer,
        topic_owner_user_id=topic_owner_user_id,
    )
    staff = can_moderate_discussion(db, course_id, viewer) or (
        topic_owner_user_id is not None
        and can_moderate_free_topic_as_owner(db, course_id, viewer, topic_owner_user_id)
    )
    topic = db.get(DiscussionTopic, topic_id)
    exts = ", ".join(sorted(s.replace(".", "").upper() for s in ALLOWED_IMAGE_EXTENSIONS))
    topic_total_posts = count_posts_for_topic(db, topic_id)
    return {
        "discussion_thread": threaded,
        "discussion_pagination": pag,
        "can_moderate_discussion": staff,
        "discussion_ai_groups": discussion_ai_group_options(db, topic) if topic else [],
        "discussion_topic_post_count": topic_total_posts,
        "discussion_image_rules_en": (
            f"Images: {exts}; max {human_upload_max_bytes()} per file after processing; "
            f"up to {DISCUSSION_MAX_IMAGES_PER_POST} images per post. No remote hotlinks."
        ),
        "discussion_image_rules_zh": (
            f"图片：格式 {exts}；处理后单文件不超过 {human_upload_max_bytes()}；"
            f"每条帖子最多 {DISCUSSION_MAX_IMAGES_PER_POST} 张。禁止外链图片。"
        ),
    }


def parse_discussion_query(request: object) -> tuple[int | None, int | None]:
    """Parse ?page= and ?post= from a Starlette/FastAPI request."""
    qp = getattr(request, "query_params", None)
    if qp is None:
        return (None, None)
    page_s = qp.get("page")
    post_s = qp.get("post")
    page_p: int | None = None
    post_p: int | None = None
    if page_s and str(page_s).isdigit():
        page_p = int(page_s)
    if post_s and str(post_s).isdigit():
        post_p = int(post_s)
    return (page_p, post_p)


def discussion_pagination_state(
    db: Session,
    *,
    topic_id: int,
    page_size: int,
    page: int | None,
    anchor_post_id: int | None,
) -> dict:
    """Compute offset/limit/total/page for a discussion topic (stable created_at order)."""
    total = count_posts_for_topic(db, topic_id)
    ps = max(10, min(200, int(page_size)))
    if total == 0:
        return {
            "total": 0,
            "page_size": ps,
            "page": 1,
            "total_pages": 1,
            "offset": 0,
            "has_older": False,
            "has_newer": False,
        }
    total_pages = max(1, (total + ps - 1) // ps)
    offset = 0
    current_page = 1
    if anchor_post_id is not None:
        rank = db.scalar(
            select(func.count())
            .select_from(DiscussionPost)
            .where(
                DiscussionPost.topic_id == topic_id,
                DiscussionPost.deleted_at.is_(None),
                DiscussionPost.id < anchor_post_id,
            )
        )
        r = int(rank or 0)
        current_page = min(total_pages, max(1, r // ps + 1))
        offset = (current_page - 1) * ps
    elif page is not None:
        current_page = min(total_pages, max(1, int(page)))
        offset = (current_page - 1) * ps
    else:
        current_page = total_pages
        offset = max(0, total - ps)
    has_newer = offset + ps < total
    has_older = offset > 0
    return {
        "total": total,
        "page_size": ps,
        "page": current_page,
        "total_pages": total_pages,
        "offset": offset,
        "has_older": has_older,
        "has_newer": has_newer,
    }


def create_post(
    db: Session,
    *,
    topic_id: int,
    author: User,
    body: str,
    parent_post_id: int | None,
    is_anonymous: bool,
    is_ai: bool = False,
    has_pending_image_uploads: bool = False,
) -> DiscussionPost:
    body = (body or "").strip()
    if not body and not has_pending_image_uploads:
        raise ValueError("empty_body")
    if not body and has_pending_image_uploads:
        body = "(image)"
    if len(body) > DISCUSSION_BODY_MAX_CHARS:
        raise ValueError("body_too_large")
    if has_disallowed_remote_image_markdown(body):
        raise ValueError("remote_images_not_allowed")

    topic = db.get(DiscussionTopic, topic_id)
    if topic is None:
        raise ValueError("invalid_topic")
    if not is_ai and is_user_muted_for_course(db, topic.course_id, author.id):
        raise ValueError("user_muted")

    if parent_post_id is not None:
        parent = db.get(DiscussionPost, parent_post_id)
        if parent is None or parent.topic_id != topic_id or parent.deleted_at is not None:
            raise ValueError("invalid_parent_post")
        if topic.kind == DiscussionTopicKind.FREE_DISCUSSION_TOPIC and parent.parent_post_id is not None:
            raise ValueError("nested_reply_not_allowed")

    post = DiscussionPost(
        topic_id=topic_id,
        author_id=author.id,
        parent_post_id=parent_post_id,
        body_text=body,
        is_anonymous=bool(is_anonymous),
        is_ai=bool(is_ai),
    )
    db.add(post)
    db.flush()
    return post
