"""Discussion group cards, membership, visibility, and moderation permissions."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.auth import is_admin, is_super_admin
from app.constants import (
    DiscussionGroupMemberRole,
    DiscussionGroupStatus,
    DiscussionGroupVisibility,
    DiscussionTopicKind,
    MembershipStatus,
    ReportStatus,
)
from app.db import utcnow
from app.models import (
    DiscussionGroup,
    DiscussionGroupAuditLog,
    DiscussionGroupMember,
    DiscussionPost,
    DiscussionTopic,
    Report,
    User,
)
from app.services.discussions import attach_avatar_and_role_badges, flat_thread_for_template
from app.services.free_discussion import get_open_community_course


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def group_cover_url(group: DiscussionGroup) -> str | None:
    return f"/data-files/{quote(str(group.cover_image_path), safe='/')}" if group.cover_image_path else None


def get_group_member(db: Session, group_id: int, user_id: int) -> DiscussionGroupMember | None:
    return db.scalar(
        select(DiscussionGroupMember).where(
            DiscussionGroupMember.group_id == group_id,
            DiscussionGroupMember.user_id == user_id,
            DiscussionGroupMember.status == MembershipStatus.ACTIVE,
        )
    )


def is_group_member(db: Session, group_id: int, user_id: int) -> bool:
    return get_group_member(db, group_id, user_id) is not None


def can_manage_group(db: Session, group: DiscussionGroup, user: User) -> bool:
    member = get_group_member(db, group.id, user.id)
    return bool(member and member.role == DiscussionGroupMemberRole.OWNER)


def private_group_has_active_evidence_report(db: Session, group_id: int) -> Report | None:
    return db.scalar(
        select(Report)
        .where(
            Report.private_discussion_group_id == group_id,
            Report.status.in_([ReportStatus.PENDING, ReportStatus.REVIEWING]),
        )
        .order_by(Report.created_at.asc())
        .limit(1)
    )


def can_super_admin_review_private_group(db: Session, group: DiscussionGroup, user: User) -> bool:
    return bool(
        is_super_admin(user)
        and private_group_has_active_evidence_report(db, group.id) is not None
    )


def can_view_group(db: Session, group: DiscussionGroup, user: User, *, via_report: bool = False) -> bool:
    if get_group_member(db, group.id, user.id) is not None:
        return True
    if group.visibility == DiscussionGroupVisibility.PUBLIC:
        return True
    if via_report and can_super_admin_review_private_group(db, group, user):
        return True
    return False


def can_moderate_group(db: Session, group: DiscussionGroup, user: User, *, via_report: bool = False) -> bool:
    if can_manage_group(db, group, user):
        return True
    if group.visibility == DiscussionGroupVisibility.PUBLIC and is_admin(user):
        return True
    if via_report and can_super_admin_review_private_group(db, group, user):
        return True
    return False


def can_post_in_group(db: Session, group: DiscussionGroup, user: User) -> bool:
    if group.status != DiscussionGroupStatus.ACTIVE:
        return False
    member = get_group_member(db, group.id, user.id)
    if member is None:
        return False
    until = _as_utc(member.muted_until)
    return until is None or until <= utcnow()


def create_group(db: Session, *, creator: User, title: str, description: str, visibility: str) -> DiscussionGroup:
    title = (title or "").strip()
    if not title:
        raise ValueError("empty_title")
    vis = DiscussionGroupVisibility.PUBLIC if visibility == DiscussionGroupVisibility.PUBLIC.value else DiscussionGroupVisibility.PRIVATE
    group = DiscussionGroup(created_by=creator.id, title=title, description=(description or "").strip() or None, visibility=vis)
    db.add(group)
    db.flush()
    db.add(
        DiscussionGroupMember(
            group_id=group.id,
            user_id=creator.id,
            role=DiscussionGroupMemberRole.OWNER,
            status=MembershipStatus.ACTIVE,
        )
    )
    oc = get_open_community_course(db)
    if oc is None:
        raise ValueError("open_community_missing")
    db.add(
        DiscussionTopic(
            course_id=oc.id,
            kind=DiscussionTopicKind.DISCUSSION_GROUP,
            discussion_group_id=group.id,
        )
    )
    db.flush()
    return group


def update_group_card(
    db: Session,
    group: DiscussionGroup,
    *,
    title: str,
    description: str,
    visibility: str,
) -> None:
    title = (title or "").strip()
    if not title:
        raise ValueError("empty_title")
    group.title = title
    group.description = (description or "").strip() or None
    group.visibility = (
        DiscussionGroupVisibility.PUBLIC
        if visibility == DiscussionGroupVisibility.PUBLIC.value
        else DiscussionGroupVisibility.PRIVATE
    )
    group.updated_at = utcnow()
    db.flush()


def list_visible_groups(db: Session, user: User) -> list[DiscussionGroup]:
    stmt = (
        select(DiscussionGroup)
        .options(joinedload(DiscussionGroup.creator))
        .outerjoin(
            DiscussionGroupMember,
            (DiscussionGroupMember.group_id == DiscussionGroup.id)
            & (DiscussionGroupMember.user_id == user.id)
            & (DiscussionGroupMember.status == MembershipStatus.ACTIVE),
        )
        .where(
            or_(
                DiscussionGroup.visibility == DiscussionGroupVisibility.PUBLIC,
                DiscussionGroupMember.id.is_not(None),
            )
        )
        .order_by(DiscussionGroup.created_at.desc())
    )
    return list(db.scalars(stmt).unique().all())


def list_group_members(db: Session, group_id: int) -> list[DiscussionGroupMember]:
    return list(
        db.scalars(
            select(DiscussionGroupMember)
            .options(joinedload(DiscussionGroupMember.user))
            .where(
                DiscussionGroupMember.group_id == group_id,
                DiscussionGroupMember.status == MembershipStatus.ACTIVE,
            )
            .order_by(DiscussionGroupMember.joined_at.asc())
        ).all()
    )


def add_group_member_by_username(db: Session, group: DiscussionGroup, username: str) -> User:
    username = (username or "").strip()
    user = db.scalar(select(User).where(User.username == username, User.is_active.is_(True), User.email_verified.is_(True)))
    if user is None:
        raise ValueError("user_not_found")
    existing = db.scalar(
        select(DiscussionGroupMember).where(
            DiscussionGroupMember.group_id == group.id,
            DiscussionGroupMember.user_id == user.id,
        )
    )
    if existing is None:
        db.add(
            DiscussionGroupMember(
                group_id=group.id,
                user_id=user.id,
                role=DiscussionGroupMemberRole.MEMBER,
                status=MembershipStatus.ACTIVE,
            )
        )
    else:
        existing.status = MembershipStatus.ACTIVE
        if existing.role != DiscussionGroupMemberRole.OWNER:
            existing.role = DiscussionGroupMemberRole.MEMBER
        existing.joined_at = utcnow()
    db.flush()
    return user


def remove_group_member(db: Session, group: DiscussionGroup, user_id: int) -> None:
    row = db.scalar(
        select(DiscussionGroupMember).where(
            DiscussionGroupMember.group_id == group.id,
            DiscussionGroupMember.user_id == user_id,
        )
    )
    if row is None or row.role == DiscussionGroupMemberRole.OWNER:
        raise ValueError("cannot_remove")
    row.status = MembershipStatus.REMOVED
    db.flush()


def set_group_member_mute(db: Session, group: DiscussionGroup, user_id: int, muted_until: datetime | None) -> None:
    row = get_group_member(db, group.id, user_id)
    if row is None or row.role == DiscussionGroupMemberRole.OWNER:
        raise ValueError("cannot_mute")
    row.muted_until = muted_until
    db.flush()


def freeze_group(db: Session, group: DiscussionGroup, actor: User, frozen: bool) -> None:
    group.status = DiscussionGroupStatus.FROZEN if frozen else DiscussionGroupStatus.ACTIVE
    group.frozen_at = utcnow() if frozen else None
    group.frozen_by_id = actor.id if frozen else None
    group.updated_at = utcnow()
    db.flush()


def visible_posts_for_group(
    db: Session,
    group: DiscussionGroup,
    viewer: User,
    *,
    via_report: bool = False,
) -> list[DiscussionPost]:
    topic = group.discussion_topic
    if topic is None:
        return []
    stmt = (
        select(DiscussionPost)
        .options(joinedload(DiscussionPost.author))
        .where(DiscussionPost.topic_id == topic.id, DiscussionPost.deleted_at.is_(None))
        .order_by(DiscussionPost.created_at.asc())
    )
    posts = list(db.scalars(stmt).all())
    if via_report and can_super_admin_review_private_group(db, group, viewer):
        return posts
    member = get_group_member(db, group.id, viewer.id)
    if member is not None:
        joined = _as_utc(member.joined_at) or utcnow()
        return [
            p
            for p in posts
            if p.visibility_snapshot == DiscussionGroupVisibility.PUBLIC.value or (_as_utc(p.created_at) or utcnow()) >= joined
        ]
    if group.visibility != DiscussionGroupVisibility.PUBLIC:
        return []
    return [p for p in posts if p.visibility_snapshot == DiscussionGroupVisibility.PUBLIC.value]


def build_group_discussion_context(
    db: Session,
    *,
    group: DiscussionGroup,
    viewer: User,
    via_report: bool = False,
) -> dict:
    posts = visible_posts_for_group(db, group, viewer, via_report=via_report)
    decorated = []
    can_mod = can_moderate_group(db, group, viewer, via_report=via_report)
    for p in posts:
        decorated.append(
            {
                "post": p,
                "display_name": "AI" if p.is_ai else p.author.username,
                "staff_hint": None,
                "can_delete": can_mod,
            }
        )
    return {
        "discussion_thread": attach_avatar_and_role_badges(
            db,
            group.discussion_topic.course_id if group.discussion_topic else 0,
            flat_thread_for_template(posts, decorated),
            viewer,
            topic_owner_user_id=group.created_by,
        ),
        "can_moderate_discussion": can_mod,
        "discussion_pagination": {"total": len(posts), "page": 1, "total_pages": 1, "has_older": False, "has_newer": False},
        "discussion_ai_groups": [],
        "discussion_topic_post_count": len(posts),
        "discussion_image_rules_en": "Images can be attached to posts.",
        "discussion_image_rules_zh": "可在发言中附加图片。",
    }


def log_private_group_review(db: Session, *, group: DiscussionGroup, actor: User, report_id: int | None, detail: str) -> None:
    db.add(
        DiscussionGroupAuditLog(
            group_id=group.id,
            actor_id=actor.id,
            action="super_admin_private_review",
            report_id=report_id,
            detail=detail,
        )
    )
