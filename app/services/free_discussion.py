"""Platform open discussion: topic cards backed by the hidden open-community course."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.auth import is_admin, is_super_admin
from app.constants import DiscussionTopicKind
from app.db import utcnow
from app.models import Course, DiscussionTopic, FreeDiscussionTopic, User
from app.services.courses import OPEN_COMMUNITY_COURSE_CODE, is_open_community_course
from app.services.discussions import create_post


def get_open_community_course(db: Session) -> Course | None:
    return db.scalar(select(Course).where(Course.code == OPEN_COMMUNITY_COURSE_CODE))


def list_free_topic_cards(db: Session) -> list[FreeDiscussionTopic]:
    return list(
        db.scalars(
            select(FreeDiscussionTopic)
            .options(joinedload(FreeDiscussionTopic.creator))
            .order_by(FreeDiscussionTopic.sort_order.asc(), FreeDiscussionTopic.id.asc())
        ).all()
    )


def get_free_topic(db: Session, topic_id: int) -> FreeDiscussionTopic | None:
    return db.scalar(
        select(FreeDiscussionTopic)
        .options(joinedload(FreeDiscussionTopic.creator))
        .where(FreeDiscussionTopic.id == topic_id)
    )


def can_manage_free_topic(db: Session, user: User | None, ft: FreeDiscussionTopic) -> bool:
    if user is None or not user.is_active:
        return False
    if is_super_admin(user) or is_admin(user):
        return True
    return ft.created_by == user.id


def create_free_topic_card(
    db: Session,
    *,
    course: Course,
    creator: User,
    title: str,
    description: str | None,
    sort_order: int = 0,
) -> FreeDiscussionTopic:
    title = (title or "").strip()
    if not title:
        raise ValueError("title_required")
    if not is_open_community_course(course):
        raise ValueError("invalid_course")
    ft = FreeDiscussionTopic(
        course_id=course.id,
        created_by=creator.id,
        title=title,
        description=(description or "").strip() or None,
        sort_order=sort_order,
        updated_at=utcnow(),
    )
    db.add(ft)
    db.flush()
    topic = DiscussionTopic(
        course_id=course.id,
        kind=DiscussionTopicKind.FREE_DISCUSSION_TOPIC,
        course_material_id=None,
        question_id=None,
        free_discussion_topic_id=ft.id,
    )
    db.add(topic)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise
    root_body = ((description or "").strip()) or "—"
    create_post(
        db,
        topic_id=topic.id,
        author=creator,
        body=root_body,
        parent_post_id=None,
        is_anonymous=False,
    )
    return ft


def update_free_topic_card(
    db: Session,
    ft: FreeDiscussionTopic,
    *,
    title: str,
    description: str | None,
    sort_order: int,
) -> None:
    t = (title or "").strip()
    if not t:
        raise ValueError("title_required")
    ft.title = t
    ft.description = (description or "").strip() or None
    ft.sort_order = sort_order
    ft.updated_at = utcnow()


def get_discussion_topic_for_free_card(db: Session, free_topic_id: int) -> DiscussionTopic | None:
    return db.scalar(
        select(DiscussionTopic).where(
            DiscussionTopic.free_discussion_topic_id == free_topic_id,
            DiscussionTopic.kind == DiscussionTopicKind.FREE_DISCUSSION_TOPIC,
        )
    )
