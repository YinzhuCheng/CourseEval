import unittest
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from PIL import Image

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
from app.config import get_settings
from app.db import Base, utcnow
from app.models import Assignment, Course, CourseMember, Question, User
from app.services.discussion_attachments import attach_discussion_images_to_post
from app.services.discussions import assignment_past_close_for_discussion, create_post, flat_thread_for_template
from app.services.image_uploads import normalize_uploaded_image
from app.services.course_materials import create_material
from app.services.post_close_reveal import reveal_bundle_for_question
from app.services.redirects import safe_local_redirect


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

    def test_flat_thread_cycle_does_not_recurse_forever(self) -> None:
        from app.constants import DiscussionTopicKind
        from app.models import DiscussionPost, DiscussionTopic

        topic = DiscussionTopic(
            course_id=self.course.id,
            kind=DiscussionTopicKind.QUESTION,
            question_id=self.q.id,
            course_material_id=None,
        )
        self.db.add(topic)
        self.db.flush()
        p1 = DiscussionPost(topic_id=topic.id, author_id=self.student.id, body_text="one", is_anonymous=False)
        p2 = DiscussionPost(topic_id=topic.id, author_id=self.teacher.id, body_text="two", is_anonymous=False)
        self.db.add_all([p1, p2])
        self.db.flush()
        p1.parent_post_id = p2.id
        p2.parent_post_id = p1.id
        self.db.commit()

        rows = [
            {"post": p1, "display_name": "s1", "staff_hint": None},
            {"post": p2, "display_name": "t1", "staff_hint": None},
        ]
        flat = flat_thread_for_template([p1, p2], rows)
        self.assertEqual(len(flat), 2)

    def test_create_post_rejects_parent_from_other_topic(self) -> None:
        from app.constants import DiscussionTopicKind
        from app.models import DiscussionPost, DiscussionTopic

        topic1 = DiscussionTopic(
            course_id=self.course.id,
            kind=DiscussionTopicKind.QUESTION,
            question_id=self.q.id,
            course_material_id=None,
        )
        topic2 = DiscussionTopic(
            course_id=self.course.id,
            kind=DiscussionTopicKind.COURSE_MATERIAL,
            question_id=None,
            course_material_id=None,
        )
        self.db.add_all([topic1, topic2])
        self.db.flush()
        parent = DiscussionPost(topic_id=topic1.id, author_id=self.student.id, body_text="root", is_anonymous=False)
        self.db.add(parent)
        self.db.flush()

        with self.assertRaises(ValueError):
            create_post(
                self.db,
                topic_id=topic2.id,
                author=self.teacher,
                body="wrong thread",
                parent_post_id=parent.id,
                is_anonymous=False,
            )

    def test_safe_local_redirect_rejects_external_targets(self) -> None:
        self.assertEqual(safe_local_redirect("https://evil.example/path", "/fallback"), "/fallback")
        self.assertEqual(safe_local_redirect("//evil.example/path", "/fallback"), "/fallback")
        self.assertEqual(safe_local_redirect("/student/questions/1?page=2", "/fallback"), "/student/questions/1?page=2")

    def test_course_material_external_url_allows_only_http_urls(self) -> None:
        with self.assertRaises(ValueError):
            create_material(
                self.db,
                course=self.course,
                title="Bad URL",
                body_markdown="",
                external_url="javascript:alert(1)",
                creator=self.teacher,
            )

        material = create_material(
            self.db,
            course=self.course,
            title="Good URL",
            body_markdown="",
            external_url="https://example.com/resource",
            creator=self.teacher,
        )
        self.assertEqual(material.external_url, "https://example.com/resource")

    def test_uploaded_image_validation_rejects_fake_or_mismatched_images(self) -> None:
        with self.assertRaises(ValueError):
            normalize_uploaded_image(b"not an image", "avatar.png")

        png = _png_bytes()
        with self.assertRaises(ValueError):
            normalize_uploaded_image(png, "avatar.jpg")

    def test_discussion_attachment_cleans_files_when_later_image_fails(self) -> None:
        from app.constants import DiscussionTopicKind
        from app.models import DiscussionPost, DiscussionTopic

        settings = get_settings()
        old_data_dir = settings.data_dir
        old_uploads_dir = settings.uploads_dir
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            object.__setattr__(settings, "data_dir", data_dir)
            object.__setattr__(settings, "uploads_dir", data_dir / "uploads")
            try:
                topic = DiscussionTopic(
                    course_id=self.course.id,
                    kind=DiscussionTopicKind.QUESTION,
                    question_id=self.q.id,
                    course_material_id=None,
                )
                self.db.add(topic)
                self.db.flush()
                post = DiscussionPost(
                    topic_id=topic.id,
                    author_id=self.student.id,
                    body_text="see attached",
                    is_anonymous=False,
                )
                self.db.add(post)
                self.db.flush()

                with self.assertRaises(ValueError):
                    attach_discussion_images_to_post(
                        self.db,
                        post,
                        self.course.id,
                        [(_png_bytes(), "ok.png"), (b"not an image", "bad.png")],
                    )
                self.assertEqual(list((data_dir / "uploads").rglob("*.*")), [])
            finally:
                object.__setattr__(settings, "data_dir", old_data_dir)
                object.__setattr__(settings, "uploads_dir", old_uploads_dir)


def _png_bytes() -> bytes:
    out = BytesIO()
    Image.new("RGB", (1, 1), color=(255, 0, 0)).save(out, format="PNG")
    return out.getvalue()


if __name__ == "__main__":
    unittest.main()
