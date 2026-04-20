import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.constants import (
    AccountRole,
    CourseStatus,
    DiscussionGroupVisibility,
    FriendRequestStatus,
    MembershipStatus,
    NotificationType,
    SocialInviteStatus,
    SocialInviteType,
)
from app.db import Base
from app.models import Course, DiscussionGroup, DiscussionGroupMember, Notification, User
from app.services.social import (
    are_friends,
    create_social_invite,
    respond_friend_request,
    respond_social_invite,
    send_direct_message,
    send_friend_request,
    unread_notification_count,
)


class SocialFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()
        self.alice = User(
            username="alice",
            email="alice@example.com",
            password_hash="x",
            account_role=AccountRole.STUDENT,
            is_active=True,
            email_verified=True,
        )
        self.bob = User(
            username="bob",
            email="bob@example.com",
            password_hash="x",
            account_role=AccountRole.STUDENT,
            is_active=True,
            email_verified=True,
        )
        self.cara = User(
            username="cara",
            email="cara@example.com",
            password_hash="x",
            account_role=AccountRole.STUDENT,
            is_active=True,
            email_verified=True,
            only_friends_can_invite_discussion_groups=True,
        )
        self.db.add_all([self.alice, self.bob, self.cara])
        self.db.flush()
        self.course = Course(code="C1", title="Course 1", status=CourseStatus.ACTIVE)
        self.group = DiscussionGroup(
            created_by=self.alice.id,
            title="Group 1",
            visibility=DiscussionGroupVisibility.PRIVATE,
        )
        self.db.add_all([self.course, self.group])
        self.db.flush()
        self.db.add(
            DiscussionGroupMember(
                group_id=self.group.id,
                user_id=self.alice.id,
                status=MembershipStatus.ACTIVE,
            )
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_friend_request_accept_creates_friendship_and_notifications(self) -> None:
        req = send_friend_request(self.db, requester=self.alice, recipient_username="bob", message="hi")
        self.db.commit()

        self.assertEqual(req.status, FriendRequestStatus.PENDING)
        self.assertEqual(unread_notification_count(self.db, self.bob.id), 1)

        respond_friend_request(self.db, request_id=req.id, recipient=self.bob, accept=True)
        self.db.commit()

        self.assertTrue(are_friends(self.db, self.alice.id, self.bob.id))
        self.assertEqual(req.status, FriendRequestStatus.ACCEPTED)
        accepted = self.db.scalar(
            select(Notification).where(
                Notification.user_id == self.alice.id,
                Notification.notification_type == NotificationType.FRIEND_ACCEPTED,
            )
        )
        self.assertIsNotNone(accepted)

    def test_direct_message_requires_friendship_and_notifies_recipient(self) -> None:
        with self.assertRaises(ValueError):
            send_direct_message(self.db, sender=self.alice, recipient_id=self.bob.id, body="blocked")

        req = send_friend_request(self.db, requester=self.alice, recipient_username="bob")
        respond_friend_request(self.db, request_id=req.id, recipient=self.bob, accept=True)
        msg = send_direct_message(self.db, sender=self.alice, recipient_id=self.bob.id, body="hello")
        self.db.commit()

        self.assertEqual(msg.body, "hello")
        notification = self.db.scalar(
            select(Notification).where(
                Notification.user_id == self.bob.id,
                Notification.notification_type == NotificationType.DIRECT_MESSAGE,
            )
        )
        self.assertIsNotNone(notification)

    def test_discussion_group_invite_accept_adds_member(self) -> None:
        invite = create_social_invite(
            self.db,
            inviter=self.alice,
            invitee_username="bob",
            invite_type=SocialInviteType.DISCUSSION_GROUP,
            target_id=self.group.id,
        )
        self.db.commit()

        respond_social_invite(self.db, invite_id=invite.id, invitee=self.bob, accept=True)
        self.db.commit()

        self.assertEqual(invite.status, SocialInviteStatus.ACCEPTED)
        member = self.db.scalar(
            select(DiscussionGroupMember).where(
                DiscussionGroupMember.group_id == self.group.id,
                DiscussionGroupMember.user_id == self.bob.id,
                DiscussionGroupMember.status == MembershipStatus.ACTIVE,
            )
        )
        self.assertIsNotNone(member)

    def test_invite_rejects_non_friend_for_group_privacy(self) -> None:
        with self.assertRaises(ValueError):
            create_social_invite(
                self.db,
                inviter=self.alice,
                invitee_username="cara",
                invite_type=SocialInviteType.DISCUSSION_GROUP,
                target_id=self.group.id,
            )


if __name__ == "__main__":
    unittest.main()
