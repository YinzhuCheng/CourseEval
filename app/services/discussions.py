"""Discussion topics, posts, and visibility rules."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.auth import is_admin, is_super_admin
from app.constants import AccountRole, AssignmentStatus, CourseRole, DiscussionTopicKind, MembershipStatus
from app.db import utcnow
from app.models import Assignment, CourseMember, DiscussionPost, DiscussionTopic, Question, User


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


def can_post_on_material_topic(db: Session, course_id: int, user: User) -> bool:
    role = viewer_course_role(db, course_id, user.id)
    return role in (CourseRole.STUDENT, CourseRole.TEACHER, CourseRole.TA)


def can_post_on_question_topic(db: Session, question: Question, user: User) -> bool:
    role = viewer_course_role(db, question.assignment.course_id, user.id)
    if role not in (CourseRole.STUDENT, CourseRole.TEACHER, CourseRole.TA):
        return False
    asn = question.assignment
    if asn.status == AssignmentStatus.DRAFT and role == CourseRole.STUDENT:
        return False
    if role in (CourseRole.TEACHER, CourseRole.TA):
        return True
    if not assignment_past_close_for_discussion(asn):
        return False
    return True


def list_posts_for_topic(db: Session, topic_id: int) -> list[DiscussionPost]:
    return list(
        db.scalars(
            select(DiscussionPost)
            .options(joinedload(DiscussionPost.author))
            .where(DiscussionPost.topic_id == topic_id)
            .order_by(DiscussionPost.created_at.asc())
        ).all()
    )


def attach_avatar_and_role_badges(db: Session, course_id: int, flat_rows: list[dict]) -> list[dict]:
    """Add avatar_url and role_badges keys for template (badges: platform + course role)."""
    from app.services.user_media import user_avatar_public_url

    roles = course_member_roles_map(db, course_id)
    for r in flat_rows:
        u = r["post"].author
        if getattr(r["post"], "is_ai", False):
            r["avatar_url"] = None
            r["role_badges"] = ["ai_assistant"]
            continue
        # Anonymous posts must not show the real user's photo (would de-anonymize).
        r["avatar_url"] = None if r["post"].is_anonymous else user_avatar_public_url(u)
        badges: list[str] = []
        if is_super_admin(u):
            badges.append("super_admin")
        elif is_admin(u):
            badges.append("admin")
        cr = roles.get(u.id)
        if cr == CourseRole.TEACHER:
            badges.append("course_teacher")
        elif cr == CourseRole.TA:
            badges.append("course_ta")
        elif u.account_role == AccountRole.TEACHER and not badges:
            badges.append("account_teacher")
        r["role_badges"] = badges
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

    # Staff: see students' and TAs' real identity when they post anonymously
    if post.author.account_role != AccountRole.TEACHER:
        return (post.author.username, "staff_student_anon")
    if post.author_id == viewer.id:
        return (post.author.username, "self_anon")
    return ("匿名教师", None)


def create_post(
    db: Session,
    *,
    topic_id: int,
    author: User,
    body: str,
    parent_post_id: int | None,
    is_anonymous: bool,
    is_ai: bool = False,
) -> DiscussionPost:
    body = (body or "").strip()
    if not body:
        raise ValueError("empty_body")
    if parent_post_id is not None:
        parent = db.get(DiscussionPost, parent_post_id)
        if parent is None or parent.topic_id != topic_id:
            raise ValueError("invalid_parent_post")
    post = DiscussionPost(
        topic_id=topic_id,
        author_id=author.id,
        parent_post_id=parent_post_id,
        body_text=body[:20000],
        is_anonymous=bool(is_anonymous),
        is_ai=bool(is_ai),
    )
    db.add(post)
    db.flush()
    return post
