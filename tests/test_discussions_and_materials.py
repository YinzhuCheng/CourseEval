import unittest
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.constants import (
    AccountRole,
    AssignmentStatus,
    CourseRole,
    CourseStatus,
    FeedbackSource,
    MembershipStatus,
    PlatformRole,
    QuestionType,
    ScoringRule,
)
from app.db import Base, utcnow
from app.models import Assignment, Course, CourseMember, Question, User
from app.services.discussions import assignment_past_close_for_discussion, flat_thread_for_template
from app.services.post_close_reveal import reveal_bundle_for_question


class DiscussionsAndMaterialsTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()

        self.teacher = User(
            username="t1",
            email="t1@example.com",
            password_hash="x",
            account_role=AccountRole.TEACHER,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.student = User(
            username="s1",
            email="s1@example.com",
            password_hash="x",
            account_role=AccountRole.STUDENT,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.db.add_all([self.teacher, self.student])
        self.db.flush()
        self.course = Course(code="CZ", title="CZ", status=CourseStatus.ACTIVE, created_by=self.teacher.id)
        self.db.add(self.course)
        self.db.flush()
        for uid, role in [(self.teacher.id, CourseRole.TEACHER), (self.student.id, CourseRole.STUDENT)]:
            self.db.add(
                CourseMember(course_id=self.course.id, user_id=uid, role=role, status=MembershipStatus.ACTIVE)
            )
        close = utcnow() - timedelta(hours=1)
        self.asn = Assignment(
            course_id=self.course.id,
            title="A1",
            status=AssignmentStatus.PUBLISHED,
            default_scoring_rule=ScoringRule.LATEST,
            close_at=close,
        )
        self.db.add(self.asn)
        self.db.flush()
        self.q = Question(
            assignment_id=self.asn.id,
            order_index=1,
            title="Q1",
            question_type=QuestionType.SHORT_ANSWER,
            max_score=Decimal("10"),
        )
        self.db.add(self.q)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_assignment_past_close(self) -> None:
        self.assertTrue(assignment_past_close_for_discussion(self.asn))

    def test_reveal_after_close_short_answer(self) -> None:
        from app.models import ShortAnswerQuestionConfig

        self.db.add(
            ShortAnswerQuestionConfig(
                question_id=self.q.id,
                rubric_text="R1",
                teacher_confirmation_required=False,
            )
        )
        self.db.commit()
        bundle = reveal_bundle_for_question(self.db, self.q)
        self.assertTrue(bundle["open"])
        self.assertEqual(bundle["rubric_text"], "R1")

    def test_flat_thread_depth(self) -> None:
        from app.models import DiscussionPost, DiscussionTopic
        from app.constants import DiscussionTopicKind

        topic = DiscussionTopic(
            course_id=self.course.id,
            kind=DiscussionTopicKind.QUESTION,
            question_id=self.q.id,
            course_material_id=None,
        )
        self.db.add(topic)
        self.db.flush()
        p1 = DiscussionPost(topic_id=topic.id, author_id=self.student.id, body_text="root", is_anonymous=False)
        self.db.add(p1)
        self.db.flush()
        p2 = DiscussionPost(
            topic_id=topic.id,
            author_id=self.teacher.id,
            parent_post_id=p1.id,
            body_text="reply",
            is_anonymous=False,
        )
        self.db.add(p2)
        self.db.commit()
        posts = [p1, p2]
        decorated = [
            {"post": p1, "display_name": "s1", "staff_hint": None},
            {"post": p2, "display_name": "t1", "staff_hint": None},
        ]
        flat = flat_thread_for_template(posts, decorated)
        self.assertEqual(flat[0]["depth"], 0)
        self.assertEqual(flat[1]["depth"], 1)


if __name__ == "__main__":
    unittest.main()
