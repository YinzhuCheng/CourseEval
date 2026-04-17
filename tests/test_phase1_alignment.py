import unittest
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import joinedload, sessionmaker

from app.constants import (
    AccountRole,
    AssignmentStatus,
    CourseRole,
    CourseStatus,
    FeedbackSource,
    LLMProvider,
    LLMScope,
    LLMTestStatus,
    MembershipStatus,
    PlatformRole,
    QuestionType,
    ScoringRule,
    SubmissionLimitMode,
    SubmissionStatus,
    UserRole,
)
from app.db import Base, utcnow
from app.models import Assignment, Course, CourseMember, Feedback, FinalGradeSnapshot, LLMConfig, Question, ShortAnswerQuestionConfig, Submission, User
from app.auth import assign_user_role, has_super_admin, resolve_registration_roles
from app.services.courses import bootstrap_sample_data
from app.services.permissions import can_manage_course, can_staff_course, is_platform_admin
from app.services.submissions import (
    _resolve_llm_config_for_question,
    _strip_hidden_output_sections,
    create_short_answer_submission,
    is_submission_pending_teacher_review,
    resolve_submission_score,
    update_final_grade_snapshot,
)


class Phase1AlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        self.db = session_factory()

        self.teacher = User(
            username="teacher",
            email="teacher@example.com",
            password_hash="x",
            account_role=AccountRole.TEACHER,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.ta = User(
            username="ta",
            email="ta@example.com",
            password_hash="x",
            account_role=AccountRole.TEACHER,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.student = User(
            username="student",
            email="student@example.com",
            password_hash="x",
            account_role=AccountRole.STUDENT,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.db.add_all([self.teacher, self.ta, self.student])
        self.db.flush()

        self.course = Course(
            code="C1",
            title="Course 1",
            status=CourseStatus.ACTIVE,
            created_by=self.teacher.id,
        )
        self.db.add(self.course)
        self.db.flush()
        self.db.add_all(
            [
                CourseMember(
                    course_id=self.course.id,
                    user_id=self.teacher.id,
                    role=CourseRole.TEACHER,
                    status=MembershipStatus.ACTIVE,
                ),
                CourseMember(
                    course_id=self.course.id,
                    user_id=self.ta.id,
                    role=CourseRole.TA,
                    status=MembershipStatus.ACTIVE,
                ),
                CourseMember(
                    course_id=self.course.id,
                    user_id=self.student.id,
                    role=CourseRole.STUDENT,
                    status=MembershipStatus.ACTIVE,
                ),
            ]
        )

        self.assignment = Assignment(
            course_id=self.course.id,
            title="Assignment 1",
            status=AssignmentStatus.PUBLISHED,
            allow_late=True,
            default_scoring_rule=ScoringRule.LATEST,
            submission_limit_mode=SubmissionLimitMode.UNLIMITED,
        )
        self.db.add(self.assignment)
        self.db.flush()

        self.question = Question(
            assignment_id=self.assignment.id,
            order_index=1,
            title="Short answer",
            question_type=QuestionType.SHORT_ANSWER,
            max_score=Decimal("20"),
        )
        self.db.add(self.question)
        self.db.flush()
        self.db.add(
            ShortAnswerQuestionConfig(
                question_id=self.question.id,
                min_length=1,
                teacher_confirmation_required=True,
            )
        )
        self.db.commit()
        self.db.refresh(self.course)
        self.db.refresh(self.question)

    def tearDown(self) -> None:
        self.db.close()

    def test_short_answer_submission_waits_for_teacher_confirmation(self) -> None:
        submission = create_short_answer_submission(
            self.db,
            user_id=self.student.id,
            question=self.question,
            answer_text="Pending review answer",
        )

        snapshot = self.db.scalar(
            select(FinalGradeSnapshot).where(
                FinalGradeSnapshot.student_id == self.student.id,
                FinalGradeSnapshot.question_id == self.question.id,
            )
        )

        self.assertEqual(submission.status, SubmissionStatus.SUBMITTED)
        self.assertFalse(submission.is_effective_submission)
        self.assertIsNone(submission.completed_at)
        self.assertTrue(submission.counts_toward_limit)
        self.assertTrue(is_submission_pending_teacher_review(submission))
        self.assertIsNone(snapshot)

    def test_pending_short_answer_does_not_treat_llm_suggestion_as_effective_score(self) -> None:
        submission = create_short_answer_submission(
            self.db,
            user_id=self.student.id,
            question=self.question,
            answer_text="Answer that gets an LLM suggestion",
        )
        self.db.add(
            Feedback(
                submission_id=submission.id,
                source=FeedbackSource.LLM,
                score_suggestion=Decimal("18"),
                comment_text="Suggested score only.",
            )
        )
        self.db.commit()
        pending_submission = self.db.scalar(
            select(Submission)
            .options(
                joinedload(Submission.question).joinedload(Question.short_answer_config),
                joinedload(Submission.feedback_items),
                joinedload(Submission.evaluation_results),
            )
            .where(Submission.id == submission.id)
        )
        assert pending_submission is not None

        score, source = resolve_submission_score(pending_submission)

        self.assertIsNone(score)
        self.assertIsNone(source)
        self.assertTrue(is_submission_pending_teacher_review(pending_submission))

    def test_teacher_feedback_activates_snapshot(self) -> None:
        submission = create_short_answer_submission(
            self.db,
            user_id=self.student.id,
            question=self.question,
            answer_text="Answer to be graded",
        )
        self.db.add(
            Feedback(
                submission_id=submission.id,
                source=FeedbackSource.TEACHER,
                score_suggestion=Decimal("17"),
                created_by=self.teacher.id,
                comment_text="Confirmed final score.",
            )
        )
        submission.status = SubmissionStatus.COMPLETED
        submission.completed_at = utcnow()
        submission.is_effective_submission = True
        self.db.commit()

        graded_submission = self.db.scalar(
            select(Submission)
            .options(
                joinedload(Submission.question).joinedload(Question.short_answer_config),
                joinedload(Submission.feedback_items),
                joinedload(Submission.evaluation_results),
            )
            .where(Submission.id == submission.id)
        )
        assert graded_submission is not None

        update_final_grade_snapshot(self.db, graded_submission)

        snapshot = self.db.scalar(
            select(FinalGradeSnapshot).where(
                FinalGradeSnapshot.student_id == self.student.id,
                FinalGradeSnapshot.question_id == self.question.id,
            )
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(Decimal(str(snapshot.score)), Decimal("17"))
        self.assertEqual(snapshot.feedback_source, FeedbackSource.TEACHER)
        self.assertEqual(snapshot.effective_submission_id, submission.id)

    def test_ta_is_staff_but_not_course_manager(self) -> None:
        self.assertTrue(can_staff_course(self.db, self.course, self.ta))
        self.assertFalse(can_manage_course(self.db, self.course, self.ta))
        self.assertTrue(can_manage_course(self.db, self.course, self.teacher))

    def test_hidden_test_output_is_removed_from_student_logs(self) -> None:
        raw_output = (
            "Notebook stdout\n"
            "=== Visible Tests ===\n"
            "visible result\n"
            "=== Hidden Tests ===\n"
            "secret branch\n"
            "secret details\n"
        )

        sanitized, changed = _strip_hidden_output_sections(raw_output, {"=== Hidden Tests ==="})

        self.assertTrue(changed)
        self.assertIn("visible result", sanitized)
        self.assertNotIn("secret branch", sanitized)
        self.assertNotIn("secret details", sanitized)

    def test_bootstrap_data_structures_course_contains_three_new_modes(self) -> None:
        bootstrap_user = User(
            username="default",
            email="default@example.com",
            password_hash="x",
            account_role=AccountRole.TEACHER,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.db.add(bootstrap_user)
        self.db.commit()
        self.db.refresh(bootstrap_user)

        bootstrap_sample_data(self.db, bootstrap_user)

        course = self.db.scalar(select(Course).where(Course.title == "数据结构"))
        self.assertIsNotNone(course)
        assert course is not None
        assignment = self.db.scalar(select(Assignment).where(Assignment.course_id == course.id))
        self.assertIsNotNone(assignment)
        assert assignment is not None
        questions = list(
            self.db.scalars(
                select(Question).where(Question.assignment_id == assignment.id).order_by(Question.order_index.asc())
            )
        )
        self.assertEqual(
            [question.question_type for question in questions],
            [
                QuestionType.PYTHON_CODE,
                QuestionType.PDF_LLM,
                QuestionType.FORMATTED_TEXT_LLM,
            ],
        )
        pdf_question = questions[1]
        formatted_question = questions[2]
        self.assertIsNotNone(pdf_question.file_question_config)
        self.assertIsNotNone(formatted_question.file_question_config)
        assert pdf_question.file_question_config is not None
        assert formatted_question.file_question_config is not None
        self.assertTrue(pdf_question.file_question_config.llm_suggestion_enabled)
        self.assertTrue(formatted_question.file_question_config.llm_suggestion_enabled)

    def test_course_can_follow_latest_platform_llm_default(self) -> None:
        platform_llm = LLMConfig(
            scope=LLMScope.PLATFORM,
            name="Platform default",
            provider_type=LLMProvider.OPENAI_COMPATIBLE,
            base_url="https://llm.example.com",
            api_key="secret",
            model_name="gpt-test",
            enabled=True,
            last_test_status=LLMTestStatus.SUCCESS,
            last_tested_at=utcnow(),
        )
        self.db.add(platform_llm)
        self.db.commit()
        self.db.refresh(platform_llm)

        self.course.use_global_llm_default = True
        self.course.default_llm_config_id = None
        self.db.commit()
        self.db.refresh(self.course)
        self.db.refresh(self.assignment)
        self.db.refresh(self.question)

        resolved = _resolve_llm_config_for_question(self.question, self.db)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.id, platform_llm.id)

    def test_course_specific_llm_override_beats_global_default(self) -> None:
        platform_llm = LLMConfig(
            scope=LLMScope.PLATFORM,
            name="Platform default",
            provider_type=LLMProvider.OPENAI_COMPATIBLE,
            base_url="https://llm.example.com",
            api_key="secret",
            model_name="gpt-platform",
            enabled=True,
            last_test_status=LLMTestStatus.SUCCESS,
            last_tested_at=utcnow(),
        )
        course_llm = LLMConfig(
            scope=LLMScope.PLATFORM,
            name="Course override",
            provider_type=LLMProvider.OPENAI_COMPATIBLE,
            base_url="https://llm.example.com",
            api_key="secret",
            model_name="gpt-course",
            enabled=True,
            last_test_status=LLMTestStatus.SUCCESS,
            last_tested_at=utcnow(),
        )
        self.db.add_all([platform_llm, course_llm])
        self.db.commit()
        self.db.refresh(course_llm)

        self.course.use_global_llm_default = False
        self.course.default_llm_config_id = course_llm.id
        self.db.commit()
        self.db.refresh(self.course)
        self.db.refresh(self.assignment)
        self.db.refresh(self.question)

        resolved = _resolve_llm_config_for_question(self.question, self.db)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.id, course_llm.id)

    def test_first_registration_becomes_super_admin_once(self) -> None:
        fresh_user = User(
            username="fresh",
            email="fresh@example.com",
            password_hash="x",
            account_role=AccountRole.STUDENT,
            platform_role=PlatformRole.USER,
            is_active=True,
        )
        self.db.add(fresh_user)
        self.db.commit()

        self.assertFalse(has_super_admin(self.db))
        account_role, platform_role = resolve_registration_roles(self.db)
        self.assertEqual(account_role, AccountRole.STUDENT)
        self.assertEqual(platform_role, PlatformRole.SUPER_ADMIN)

        assign_user_role(fresh_user, UserRole.SUPER_ADMIN)
        self.db.commit()

        self.assertTrue(has_super_admin(self.db))
        next_account_role, next_platform_role = resolve_registration_roles(self.db)
        self.assertEqual(next_account_role, AccountRole.STUDENT)
        self.assertEqual(next_platform_role, PlatformRole.USER)

    def test_assign_user_role_maps_storage_fields_consistently(self) -> None:
        user = self.student

        assign_user_role(user, UserRole.TEACHER)
        self.assertEqual(user.account_role, AccountRole.TEACHER)
        self.assertEqual(user.platform_role, PlatformRole.USER)
        self.assertEqual(user.effective_role, UserRole.TEACHER)

        assign_user_role(user, UserRole.ADMIN)
        self.assertEqual(user.account_role, AccountRole.STUDENT)
        self.assertEqual(user.platform_role, PlatformRole.ADMIN)
        self.assertEqual(user.effective_role, UserRole.ADMIN)

        assign_user_role(user, UserRole.STUDENT)
        self.assertEqual(user.account_role, AccountRole.STUDENT)
        self.assertEqual(user.platform_role, PlatformRole.USER)
        self.assertEqual(user.effective_role, UserRole.STUDENT)

    def test_legacy_administrator_account_remains_admin_effective_role(self) -> None:
        legacy_admin = User(
            username="legacy-admin",
            email="legacy-admin@example.com",
            password_hash="x",
            account_role=AccountRole.ADMINISTRATOR,
            platform_role=PlatformRole.ADMIN,
            email_verified=True,
            is_active=True,
        )
        self.assertEqual(legacy_admin.effective_role, UserRole.ADMIN)
        self.assertTrue(is_platform_admin(legacy_admin))


if __name__ == "__main__":
    unittest.main()
