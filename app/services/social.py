"""Friends, direct messages, invitations, and in-app notifications."""

from __future__ import annotations

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.auth import is_admin
from app.constants import (
    CourseRole,
    CourseStatus,
    DiscussionGroupMemberRole,
    DiscussionGroupStatus,
    FriendRequestStatus,
    MembershipStatus,
    NotificationType,
    SocialInviteStatus,
    SocialInviteType,
)
from app.db import utcnow
from app.models import (
    Course,
    CourseMember,
    DirectMessage,
    DirectMessageThread,
    DiscussionGroup,
    DiscussionGroupMember,
    FreeDiscussionTopic,
    FriendRequest,
    Friendship,
    Notification,
    SocialInvite,
    User,
)


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def get_active_verified_user_by_username(db: Session, username: str) -> User | None:
    return db.scalar(
        select(User).where(
            func.lower(User.username) == (username or "").strip().lower(),
            User.is_active.is_(True),
            User.email_verified.is_(True),
        )
    )


def are_friends(db: Session, user_a_id: int, user_b_id: int) -> bool:
    if user_a_id == user_b_id:
        return False
    low, high = _pair(user_a_id, user_b_id)
    return db.scalar(select(Friendship.id).where(Friendship.user_low_id == low, Friendship.user_high_id == high)) is not None


def create_notification(
    db: Session,
    *,
    user_id: int,
    notification_type: NotificationType,
    source_type: str,
    source_id: int | None,
    target_type: str,
    target_id: int | None,
    title: str,
    body: str | None = None,
    actor_id: int | None = None,
    link_url: str | None = None,
) -> Notification:
    notification = Notification(
        user_id=user_id,
        actor_id=actor_id,
        notification_type=notification_type,
        source_type=source_type,
        source_id=source_id,
        target_type=target_type,
        target_id=target_id,
        title=title,
        body=body,
        link_url=link_url,
    )
    db.add(notification)
    db.flush()
    return notification


def unread_notification_count(db: Session, user_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(Notification.id)).where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        or 0
    )


def list_notifications(db: Session, user_id: int, *, category: str = "all") -> list[Notification]:
    stmt = (
        select(Notification)
        .options(joinedload(Notification.actor))
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(100)
    )
    if category == "friends":
        stmt = stmt.where(Notification.notification_type.in_([NotificationType.FRIEND_REQUEST, NotificationType.FRIEND_ACCEPTED]))
    elif category == "messages":
        stmt = stmt.where(Notification.notification_type == NotificationType.DIRECT_MESSAGE)
    elif category == "courses":
        stmt = stmt.where(Notification.notification_type == NotificationType.COURSE_INVITE)
    elif category == "discussion":
        stmt = stmt.where(
            Notification.notification_type.in_(
                [NotificationType.FREE_DISCUSSION_TOPIC_INVITE, NotificationType.DISCUSSION_GROUP_INVITE]
            )
        )
    return list(db.scalars(stmt).all())


def mark_notifications_read(db: Session, user_id: int, notification_ids: list[int] | None = None) -> int:
    now = utcnow()
    stmt = select(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    if notification_ids:
        stmt = stmt.where(Notification.id.in_(notification_ids))
    rows = list(db.scalars(stmt).all())
    for row in rows:
        row.read_at = now
    db.flush()
    return len(rows)


def send_friend_request(db: Session, *, requester: User, recipient_username: str, message: str = "") -> FriendRequest:
    recipient = get_active_verified_user_by_username(db, recipient_username)
    if recipient is None:
        raise ValueError("user_not_found")
    if recipient.id == requester.id:
        raise ValueError("cannot_friend_self")
    if are_friends(db, requester.id, recipient.id):
        raise ValueError("already_friends")
    existing = db.scalar(
        select(FriendRequest).where(
            FriendRequest.status == FriendRequestStatus.PENDING,
            or_(
                and_(FriendRequest.requester_id == requester.id, FriendRequest.recipient_id == recipient.id),
                and_(FriendRequest.requester_id == recipient.id, FriendRequest.recipient_id == requester.id),
            ),
        )
    )
    if existing is not None:
        raise ValueError("request_pending")
    request = FriendRequest(
        requester_id=requester.id,
        recipient_id=recipient.id,
        message=(message or "").strip()[:500] or None,
    )
    db.add(request)
    db.flush()
    create_notification(
        db,
        user_id=recipient.id,
        actor_id=requester.id,
        notification_type=NotificationType.FRIEND_REQUEST,
        source_type="user",
        source_id=requester.id,
        target_type="friend_request",
        target_id=request.id,
        title=f"{requester.username} sent you a friend request.",
        body=request.message,
        link_url="/me/friends/requests",
    )
    return request


def respond_friend_request(db: Session, *, request_id: int, recipient: User, accept: bool) -> FriendRequest:
    request = db.get(FriendRequest, request_id)
    if request is None or request.recipient_id != recipient.id or request.status != FriendRequestStatus.PENDING:
        raise ValueError("request_not_found")
    request.status = FriendRequestStatus.ACCEPTED if accept else FriendRequestStatus.REJECTED
    request.responded_at = utcnow()
    if accept:
        low, high = _pair(request.requester_id, request.recipient_id)
        existing = db.scalar(select(Friendship).where(Friendship.user_low_id == low, Friendship.user_high_id == high))
        if existing is None:
            db.add(Friendship(user_low_id=low, user_high_id=high))
        create_notification(
            db,
            user_id=request.requester_id,
            actor_id=recipient.id,
            notification_type=NotificationType.FRIEND_ACCEPTED,
            source_type="user",
            source_id=recipient.id,
            target_type="friendship",
            target_id=recipient.id,
            title=f"{recipient.username} accepted your friend request.",
            link_url="/me/friends",
        )
    db.flush()
    return request


def remove_friend(db: Session, *, user: User, friend_id: int) -> None:
    low, high = _pair(user.id, friend_id)
    row = db.scalar(select(Friendship).where(Friendship.user_low_id == low, Friendship.user_high_id == high))
    if row is None:
        raise ValueError("friendship_not_found")
    db.delete(row)
    db.flush()


def list_friends(db: Session, user_id: int) -> list[User]:
    rows = list(
        db.scalars(
            select(Friendship)
            .where(or_(Friendship.user_low_id == user_id, Friendship.user_high_id == user_id))
            .order_by(Friendship.created_at.desc())
        ).all()
    )
    friend_ids = [row.user_high_id if row.user_low_id == user_id else row.user_low_id for row in rows]
    if not friend_ids:
        return []
    users = list(db.scalars(select(User).where(User.id.in_(friend_ids)).order_by(User.username.asc())).all())
    order = {uid: idx for idx, uid in enumerate(friend_ids)}
    return sorted(users, key=lambda item: order.get(item.id, 0))


def list_received_friend_requests(db: Session, user_id: int) -> list[FriendRequest]:
    return list(
        db.scalars(
            select(FriendRequest)
            .options(joinedload(FriendRequest.requester))
            .where(FriendRequest.recipient_id == user_id, FriendRequest.status == FriendRequestStatus.PENDING)
            .order_by(FriendRequest.created_at.desc())
        ).all()
    )


def list_sent_friend_requests(db: Session, user_id: int) -> list[FriendRequest]:
    return list(
        db.scalars(
            select(FriendRequest)
            .options(joinedload(FriendRequest.recipient))
            .where(FriendRequest.requester_id == user_id, FriendRequest.status == FriendRequestStatus.PENDING)
            .order_by(FriendRequest.created_at.desc())
        ).all()
    )


def search_users_for_friend_request(db: Session, *, viewer: User, query: str) -> list[User]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    pattern = f"%{q.lower()}%"
    return list(
        db.scalars(
            select(User)
            .where(
                User.id != viewer.id,
                User.is_active.is_(True),
                User.email_verified.is_(True),
                or_(func.lower(User.username).like(pattern), func.lower(User.email).like(pattern)),
            )
            .order_by(User.username.asc())
            .limit(20)
        ).all()
    )


def get_or_create_message_thread(db: Session, user_a_id: int, user_b_id: int) -> DirectMessageThread:
    low, high = _pair(user_a_id, user_b_id)
    thread = db.scalar(
        select(DirectMessageThread).where(
            DirectMessageThread.user_low_id == low,
            DirectMessageThread.user_high_id == high,
        )
    )
    if thread is None:
        thread = DirectMessageThread(user_low_id=low, user_high_id=high)
        db.add(thread)
        db.flush()
    return thread


def send_direct_message(db: Session, *, sender: User, recipient_id: int, body: str) -> DirectMessage:
    if not are_friends(db, sender.id, recipient_id):
        raise ValueError("not_friends")
    text = (body or "").strip()
    if not text:
        raise ValueError("empty_message")
    if len(text) > 5000:
        raise ValueError("message_too_long")
    recipient = db.get(User, recipient_id)
    if recipient is None or not recipient.is_active:
        raise ValueError("user_not_found")
    thread = get_or_create_message_thread(db, sender.id, recipient.id)
    msg = DirectMessage(thread_id=thread.id, sender_id=sender.id, body=text)
    db.add(msg)
    now = utcnow()
    thread.last_message_at = now
    if sender.id == thread.user_low_id:
        thread.user_low_read_at = now
    else:
        thread.user_high_read_at = now
    db.flush()
    create_notification(
        db,
        user_id=recipient.id,
        actor_id=sender.id,
        notification_type=NotificationType.DIRECT_MESSAGE,
        source_type="user",
        source_id=sender.id,
        target_type="direct_message_thread",
        target_id=thread.id,
        title=f"{sender.username} sent you a message.",
        body=text[:200],
        link_url=f"/me/messages/threads/{thread.id}",
    )
    return msg


def list_message_threads(db: Session, user_id: int) -> list[dict]:
    threads = list(
        db.scalars(
            select(DirectMessageThread)
            .options(joinedload(DirectMessageThread.user_low), joinedload(DirectMessageThread.user_high))
            .where(or_(DirectMessageThread.user_low_id == user_id, DirectMessageThread.user_high_id == user_id))
            .order_by(desc(DirectMessageThread.last_message_at), desc(DirectMessageThread.created_at))
        ).all()
    )
    result = []
    for thread in threads:
        other = thread.user_high if thread.user_low_id == user_id else thread.user_low
        read_at = thread.user_low_read_at if thread.user_low_id == user_id else thread.user_high_read_at
        unread_conditions = [
            DirectMessage.thread_id == thread.id,
            DirectMessage.sender_id != user_id,
        ]
        if read_at is not None:
            unread_conditions.append(DirectMessage.created_at > read_at)
        unread = db.scalar(
            select(func.count(DirectMessage.id)).where(
                *unread_conditions,
            )
        )
        result.append({"thread": thread, "other_user": other, "unread_count": int(unread or 0)})
    return result


def get_message_thread_for_user(db: Session, *, thread_id: int, user: User) -> DirectMessageThread:
    thread = db.get(DirectMessageThread, thread_id)
    if thread is None or user.id not in (thread.user_low_id, thread.user_high_id):
        raise ValueError("thread_not_found")
    now = utcnow()
    if user.id == thread.user_low_id:
        thread.user_low_read_at = now
    else:
        thread.user_high_read_at = now
    db.flush()
    return thread


def _course_invite_allowed(db: Session, *, inviter: User, course: Course) -> bool:
    if course.status != CourseStatus.ACTIVE or course.is_hidden_from_course_lists:
        return False
    if is_admin(inviter):
        return True
    row = db.scalar(
        select(CourseMember).where(
            CourseMember.course_id == course.id,
            CourseMember.user_id == inviter.id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return row is not None


def _group_invite_allowed(db: Session, *, inviter: User, group: DiscussionGroup) -> bool:
    if group.status != DiscussionGroupStatus.ACTIVE:
        return False
    if is_admin(inviter):
        return True
    row = db.scalar(
        select(DiscussionGroupMember).where(
            DiscussionGroupMember.group_id == group.id,
            DiscussionGroupMember.user_id == inviter.id,
            DiscussionGroupMember.status == MembershipStatus.ACTIVE,
        )
    )
    return row is not None


def _topic_invite_allowed(db: Session, *, inviter: User, topic: FreeDiscussionTopic) -> bool:
    if is_admin(inviter):
        return True
    return inviter.is_active and inviter.email_verified and topic is not None


def create_social_invite(
    db: Session,
    *,
    inviter: User,
    invitee_username: str,
    invite_type: SocialInviteType,
    target_id: int,
    message: str = "",
) -> SocialInvite:
    invitee = get_active_verified_user_by_username(db, invitee_username)
    if invitee is None:
        raise ValueError("user_not_found")
    if invitee.id == inviter.id:
        raise ValueError("cannot_invite_self")

    title = ""
    link_url = "/me/invites"
    notification_type = NotificationType.COURSE_INVITE
    if invite_type == SocialInviteType.COURSE:
        if not are_friends(db, inviter.id, invitee.id) and not is_admin(inviter):
            raise ValueError("not_friends")
        course = db.get(Course, target_id)
        if course is None or not _course_invite_allowed(db, inviter=inviter, course=course):
            raise ValueError("target_not_found")
        existing_member = db.scalar(
            select(CourseMember).where(
                CourseMember.course_id == course.id,
                CourseMember.user_id == invitee.id,
                CourseMember.status == MembershipStatus.ACTIVE,
            )
        )
        if existing_member is not None:
            raise ValueError("already_member")
        title = f"{inviter.username} invited you to course {course.title}."
    elif invite_type == SocialInviteType.DISCUSSION_GROUP:
        group = db.get(DiscussionGroup, target_id)
        if group is None or not _group_invite_allowed(db, inviter=inviter, group=group):
            raise ValueError("target_not_found")
        if invitee.only_friends_can_invite_discussion_groups and not are_friends(db, inviter.id, invitee.id):
            raise ValueError("privacy_restricted")
        existing_member = db.scalar(
            select(DiscussionGroupMember).where(
                DiscussionGroupMember.group_id == group.id,
                DiscussionGroupMember.user_id == invitee.id,
                DiscussionGroupMember.status == MembershipStatus.ACTIVE,
            )
        )
        if existing_member is not None:
            raise ValueError("already_member")
        title = f"{inviter.username} invited you to discussion group {group.title}."
        notification_type = NotificationType.DISCUSSION_GROUP_INVITE
    elif invite_type == SocialInviteType.FREE_DISCUSSION_TOPIC:
        if not are_friends(db, inviter.id, invitee.id) and not is_admin(inviter):
            raise ValueError("not_friends")
        topic = db.get(FreeDiscussionTopic, target_id)
        if topic is None or not _topic_invite_allowed(db, inviter=inviter, topic=topic):
            raise ValueError("target_not_found")
        title = f"{inviter.username} invited you to a discussion topic: {topic.title}."
        link_url = f"/free-discussion/topics/{topic.id}"
        notification_type = NotificationType.FREE_DISCUSSION_TOPIC_INVITE
    else:
        raise ValueError("unsupported_invite_type")

    existing = db.scalar(
        select(SocialInvite).where(
            SocialInvite.invitee_id == invitee.id,
            SocialInvite.invite_type == invite_type,
            SocialInvite.target_id == target_id,
            SocialInvite.status == SocialInviteStatus.PENDING,
        )
    )
    if existing is not None:
        raise ValueError("invite_pending")

    invite = SocialInvite(
        inviter_id=inviter.id,
        invitee_id=invitee.id,
        invite_type=invite_type,
        target_id=target_id,
        message=(message or "").strip()[:500] or None,
    )
    db.add(invite)
    db.flush()
    create_notification(
        db,
        user_id=invitee.id,
        actor_id=inviter.id,
        notification_type=notification_type,
        source_type="user",
        source_id=inviter.id,
        target_type=invite_type.value,
        target_id=target_id,
        title=title,
        body=invite.message,
        link_url=link_url,
    )
    return invite


def respond_social_invite(db: Session, *, invite_id: int, invitee: User, accept: bool) -> SocialInvite:
    invite = db.get(SocialInvite, invite_id)
    if invite is None or invite.invitee_id != invitee.id or invite.status != SocialInviteStatus.PENDING:
        raise ValueError("invite_not_found")
    invite.status = SocialInviteStatus.ACCEPTED if accept else SocialInviteStatus.REJECTED
    invite.responded_at = utcnow()
    if accept and invite.invite_type == SocialInviteType.COURSE:
        row = db.scalar(
            select(CourseMember).where(CourseMember.course_id == invite.target_id, CourseMember.user_id == invitee.id)
        )
        if row is None:
            db.add(
                CourseMember(
                    course_id=invite.target_id,
                    user_id=invitee.id,
                    role=CourseRole.STUDENT,
                    status=MembershipStatus.ACTIVE,
                )
            )
        else:
            row.status = MembershipStatus.ACTIVE
            row.role = CourseRole.STUDENT
    elif accept and invite.invite_type == SocialInviteType.DISCUSSION_GROUP:
        row = db.scalar(
            select(DiscussionGroupMember).where(
                DiscussionGroupMember.group_id == invite.target_id,
                DiscussionGroupMember.user_id == invitee.id,
            )
        )
        if row is None:
            db.add(
                DiscussionGroupMember(
                    group_id=invite.target_id,
                    user_id=invitee.id,
                    role=DiscussionGroupMemberRole.MEMBER,
                    status=MembershipStatus.ACTIVE,
                )
            )
        else:
            row.status = MembershipStatus.ACTIVE
            if row.role != DiscussionGroupMemberRole.OWNER:
                row.role = DiscussionGroupMemberRole.MEMBER
            row.joined_at = utcnow()
    db.flush()
    return invite


def list_received_invites(db: Session, user_id: int) -> list[SocialInvite]:
    return list(
        db.scalars(
            select(SocialInvite)
            .options(joinedload(SocialInvite.inviter))
            .where(SocialInvite.invitee_id == user_id)
            .order_by(SocialInvite.created_at.desc(), SocialInvite.id.desc())
            .limit(100)
        ).all()
    )


def list_sent_invites(db: Session, user_id: int) -> list[SocialInvite]:
    return list(
        db.scalars(
            select(SocialInvite)
            .options(joinedload(SocialInvite.invitee))
            .where(SocialInvite.inviter_id == user_id)
            .order_by(SocialInvite.created_at.desc(), SocialInvite.id.desc())
            .limit(100)
        ).all()
    )
