import unittest
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
    SubmissionStatus,
)
from app.db import Base, utcnow
from app.models import (
    Assignment,
    Course,
    CourseMember,
    FinalGradeSnapshot,
    Question,
    ShortAnswerQuestionConfig,
    Submission,
    User,
)
from app.services.courses import summarize_course_grade_matrix
from app.services.teacher_analytics import (
    compute_assignment_staff_stats,
    enrich_course_grade_matrix,
    grade_summary_from_float_scores,
    percentile_rank,
)


class TeacherAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        self.db = self.session_factory()

        self.teacher = User(
            username="t1",
            email="t1@example.com",
            password_hash="x",
            account_role=AccountRole.TEACHER,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.s1 = User(
            username="s1",
            email="s1@example.com",
            password_hash="x",
            account_role=AccountRole.STUDENT,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.s2 = User(
            username="s2",
            email="s2@example.com",
            password_hash="x",
            account_role=AccountRole.STUDENT,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.db.add_all([self.teacher, self.s1, self.s2])
        self.db.flush()

        self.course = Course(code="CA", title="C", status=CourseStatus.ACTIVE, created_by=self.teacher.id)
        self.db.add(self.course)
        self.db.flush()
        for uid, role in [
            (self.teacher.id, CourseRole.TEACHER),
            (self.s1.id, CourseRole.STUDENT),
            (self.s2.id, CourseRole.STUDENT),
        ]:
            self.db.add(
                CourseMember(
                    course_id=self.course.id,
                    user_id=uid,
                    role=role,
                    status=MembershipStatus.ACTIVE,
                )
            )
        asn = Assignment(
            course_id=self.course.id,
            title="HW1",
            status=AssignmentStatus.PUBLISHED,
            default_scoring_rule=ScoringRule.LATEST,
        )
        self.db.add(asn)
        self.db.flush()
        self.asn = asn
        q1 = Question(
            assignment_id=asn.id,
            order_index=1,
            title="Q1",
            question_type=QuestionType.SHORT_ANSWER,
            max_score=Decimal("10"),
        )
        q2 = Question(
            assignment_id=asn.id,
            order_index=2,
            title="Q2",
            question_type=QuestionType.SHORT_ANSWER,
            max_score=Decimal("10"),
        )
        self.db.add_all([q1, q2])
        self.db.flush()
        self.q1, self.q2 = q1, q2
        self.db.add(
            ShortAnswerQuestionConfig(
                question_id=q1.id,
                rubric_text="r",
                teacher_confirmation_required=True,
            )
        )
        self.db.add(
            ShortAnswerQuestionConfig(
                question_id=q2.id,
                rubric_text="r",
                teacher_confirmation_required=False,
            )
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_enrich_matrix_partial_vs_full(self) -> None:
        sub = Submission(
            course_id=self.course.id,
            assignment_id=self.asn.id,
            question_id=self.q1.id,
            user_id=self.s1.id,
            submission_type=QuestionType.SHORT_ANSWER,
            status=SubmissionStatus.SUBMITTED,
            submitted_at=utcnow(),
        )
        self.db.add(sub)
        self.db.commit()
        m = summarize_course_grade_matrix(self.db, self.course.id)
        enrich_course_grade_matrix(self.db, self.course.id, m)
        row_s1 = next(r for r in m["rows"] if r["student"]["id"] == self.s1.id)
        cell = row_s1["cells"][0]
        self.assertTrue(cell["any_submitted"])
        self.assertFalse(cell["fully_complete"])
        self.assertEqual(cell["questions_submitted"], 1)
        self.assertEqual(cell["question_total"], 2)

    def test_assignment_stats_mean_median(self) -> None:
        self.db.add(
            Submission(
                course_id=self.course.id,
                assignment_id=self.asn.id,
                question_id=self.q1.id,
                user_id=self.s1.id,
                submission_type=QuestionType.SHORT_ANSWER,
                status=SubmissionStatus.COMPLETED,
                submitted_at=utcnow(),
            )
        )
        self.db.add(
            Submission(
                course_id=self.course.id,
                assignment_id=self.asn.id,
                question_id=self.q1.id,
                user_id=self.s2.id,
                submission_type=QuestionType.SHORT_ANSWER,
                status=SubmissionStatus.COMPLETED,
                submitted_at=utcnow(),
            )
        )
        self.db.flush()
        for sid, sc in [(self.s1.id, Decimal("4")), (self.s2.id, Decimal("8"))]:
            self.db.add(
                FinalGradeSnapshot(
                    student_id=sid,
                    assignment_id=self.asn.id,
                    question_id=self.q1.id,
                    grading_rule_applied=ScoringRule.LATEST,
                    score=sc,
                    feedback_source=FeedbackSource.AUTO,
                )
            )
        self.db.commit()
        stats = compute_assignment_staff_stats(self.db, self.asn.id, self.course.id, [self.s1.id, self.s2.id])
        qs = stats["question_stats_by_id"][self.q1.id]
        self.assertEqual(qs["submitted_student_count"], 2)
        self.assertEqual(qs["missing_student_count"], 0)
        self.assertAlmostEqual(float(qs["score_stats"]["mean"]), 6.0)
        self.assertAlmostEqual(float(qs["score_stats"]["median"]), 6.0)

    def test_percentile_rank(self) -> None:
        vals = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(percentile_rank(vals, 25.0), 0.5)
        self.assertEqual(percentile_rank(vals, 10.0), 0.0)

    def test_grade_summary_from_float_scores(self) -> None:
        s = grade_summary_from_float_scores([1.0, 2.0, 3.0])
        self.assertEqual(s["count"], 3)
        self.assertAlmostEqual(s["mean"], 2.0)
