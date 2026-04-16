import json

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import (
    AccountRole,
    AssignmentStatus,
    CourseRole,
    CourseStatus,
    EvaluationTaskStatus,
    EvaluationTaskType,
    FeedbackSource,
    JobStatus,
    LLMProvider,
    LLMScope,
    LLMTestStatus,
    MembershipStatus,
    PlatformRole,
    QuestionType,
    RuntimeScope,
    ScoringRule,
    SubmissionLimitMode,
    SubmissionStatus,
    UserRole,
)
from app.db import Base, utcnow


def _normalize_python_test_case(item: dict, index: int) -> dict:
    expected_output = item.get("expected_output")
    if expected_output is None:
        expected_output = item.get("output", "")

    raw_points = item.get("points")
    try:
        points = float(raw_points) if raw_points not in (None, "") else 20.0
    except (TypeError, ValueError):
        points = 20.0

    return {
        "name": item.get("name") or f"Test {index}",
        "input": item.get("input", ""),
        "expected_output": expected_output,
        # Preserve the legacy key so templates and older call sites keep working.
        "output": expected_output,
        "points": points,
    }


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    account_role: Mapped[AccountRole] = mapped_column(
        Enum(AccountRole, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=AccountRole.STUDENT,
        index=True,
    )
    platform_role: Mapped[PlatformRole] = mapped_column(
        Enum(PlatformRole, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=PlatformRole.USER,
        index=True,
    )
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verification_token: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    email_verification_sent_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notebooks: Mapped[list["Notebook"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    course_memberships: Mapped[list["CourseMember"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="CourseMember.user_id",
    )
    created_courses: Mapped[list["Course"]] = relationship(
        back_populates="creator",
        foreign_keys="Course.created_by",
    )
    created_runtime_images: Mapped[list["RuntimeImage"]] = relationship(
        back_populates="creator",
        foreign_keys="RuntimeImage.created_by",
    )
    created_llm_configs: Mapped[list["LLMConfig"]] = relationship(
        back_populates="creator",
        foreign_keys="LLMConfig.created_by",
    )
    created_feedback: Mapped[list["Feedback"]] = relationship(
        back_populates="author",
        foreign_keys="Feedback.created_by",
    )
    submissions: Mapped[list["Submission"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    final_grade_snapshots: Mapped[list["FinalGradeSnapshot"]] = relationship(
        back_populates="student",
        cascade="all, delete-orphan",
        foreign_keys="FinalGradeSnapshot.student_id",
    )

    @property
    def effective_role(self) -> UserRole:
        if self.platform_role == PlatformRole.SUPER_ADMIN:
            return UserRole.SUPER_ADMIN
        if self.platform_role == PlatformRole.ADMIN:
            return UserRole.ADMIN
        if self.account_role == AccountRole.TEACHER:
            return UserRole.TEACHER
        return UserRole.STUDENT


class Notebook(Base):
    __tablename__ = "notebooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    uploaded_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="notebooks")
    jobs: Mapped[list["Job"]] = relationship(back_populates="notebook", cascade="all, delete-orphan")
    submissions: Mapped[list["Submission"]] = relationship(back_populates="notebook")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    join_code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[CourseStatus] = mapped_column(
        Enum(CourseStatus, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=CourseStatus.ACTIVE,
        index=True,
    )
    default_runtime_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtime_images.id", ondelete="SET NULL"),
        nullable=True,
    )
    default_llm_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creator: Mapped[User | None] = relationship(back_populates="created_courses", foreign_keys=[created_by])
    members: Mapped[list["CourseMember"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    assignments: Mapped[list["Assignment"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    submissions: Mapped[list["Submission"]] = relationship(back_populates="course")
    default_runtime_image: Mapped["RuntimeImage | None"] = relationship(
        back_populates="courses_using_as_default",
        foreign_keys=[default_runtime_image_id],
    )
    default_llm_config: Mapped["LLMConfig | None"] = relationship(
        back_populates="courses_using_as_default",
        foreign_keys=[default_llm_config_id],
    )
    llm_configs: Mapped[list["LLMConfig"]] = relationship(
        back_populates="course",
        foreign_keys="LLMConfig.course_id",
    )


class CourseMember(Base):
    __tablename__ = "course_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[CourseRole] = mapped_column(
        Enum(CourseRole, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        index=True,
    )
    status: Mapped[MembershipStatus] = mapped_column(
        Enum(MembershipStatus, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=MembershipStatus.ACTIVE,
        index=True,
    )
    joined_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    course: Mapped[Course] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="course_memberships", foreign_keys=[user_id])


Index("ix_course_members_course_user", CourseMember.course_id, CourseMember.user_id, unique=True)


class RuntimeImage(Base):
    __tablename__ = "runtime_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[RuntimeScope] = mapped_column(
        Enum(RuntimeScope, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=RuntimeScope.PLATFORM,
        index=True,
    )
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    image_tag: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    python_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    network_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, default=1024, nullable=False)
    cpu_limit: Mapped[str] = mapped_column(String(16), default="1", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creator: Mapped[User | None] = relationship(back_populates="created_runtime_images", foreign_keys=[created_by])
    courses_using_as_default: Mapped[list[Course]] = relationship(
        back_populates="default_runtime_image",
        foreign_keys="Course.default_runtime_image_id",
    )
    assignments_using_as_override: Mapped[list["Assignment"]] = relationship(
        back_populates="runtime_image",
        foreign_keys="Assignment.runtime_image_id",
    )
    questions_using_as_override: Mapped[list["Question"]] = relationship(
        back_populates="runtime_image",
        foreign_keys="Question.runtime_image_id",
    )
    evaluation_tasks: Mapped[list["EvaluationTask"]] = relationship(back_populates="runtime_image")


class LLMConfig(Base):
    __tablename__ = "llm_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[LLMScope] = mapped_column(
        Enum(LLMScope, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=LLMScope.PLATFORM,
        index=True,
    )
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_type: Mapped[LLMProvider] = mapped_column(
        Enum(LLMProvider, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
    )
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    temperature: Mapped[str] = mapped_column(String(16), default="0.2", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_test_status: Mapped[LLMTestStatus] = mapped_column(
        Enum(LLMTestStatus, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        default=LLMTestStatus.NEVER,
        nullable=False,
    )
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creator: Mapped[User | None] = relationship(back_populates="created_llm_configs", foreign_keys=[created_by])
    course: Mapped[Course | None] = relationship(back_populates="llm_configs", foreign_keys=[course_id])
    courses_using_as_default: Mapped[list[Course]] = relationship(
        back_populates="default_llm_config",
        foreign_keys="Course.default_llm_config_id",
    )
    assignments_using_as_override: Mapped[list["Assignment"]] = relationship(
        back_populates="llm_config",
        foreign_keys="Assignment.llm_config_id",
    )
    questions_using_as_override: Mapped[list["Question"]] = relationship(
        back_populates="llm_config",
        foreign_keys="Question.llm_config_id",
    )


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(AssignmentStatus, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=AssignmentStatus.DRAFT,
        index=True,
    )
    published_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    open_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    allow_late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_scoring_rule: Mapped[ScoringRule] = mapped_column(
        Enum(ScoringRule, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=ScoringRule.LATEST,
    )
    submission_limit_mode: Mapped[SubmissionLimitMode] = mapped_column(
        Enum(SubmissionLimitMode, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=SubmissionLimitMode.UNLIMITED,
    )
    submission_limit_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtime_images.id", ondelete="SET NULL"),
        nullable=True,
    )
    llm_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    course: Mapped[Course] = relationship(back_populates="assignments")
    questions: Mapped[list["Question"]] = relationship(back_populates="assignment", cascade="all, delete-orphan")
    submissions: Mapped[list["Submission"]] = relationship(back_populates="assignment")
    runtime_image: Mapped[RuntimeImage | None] = relationship(
        back_populates="assignments_using_as_override",
        foreign_keys=[runtime_image_id],
    )
    llm_config: Mapped[LLMConfig | None] = relationship(
        back_populates="assignments_using_as_override",
        foreign_keys=[llm_config_id],
    )
    final_grade_snapshots: Mapped[list["FinalGradeSnapshot"]] = relationship(back_populates="assignment")


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True)
    order_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_type: Mapped[QuestionType] = mapped_column(
        Enum(QuestionType, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
    )
    max_score: Mapped[Numeric] = mapped_column(Numeric(10, 2), default=100, nullable=False)
    scoring_rule_override: Mapped[ScoringRule | None] = mapped_column(
        Enum(ScoringRule, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=True,
    )
    runtime_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtime_images.id", ondelete="SET NULL"),
        nullable=True,
    )
    llm_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    assignment: Mapped[Assignment] = relationship(back_populates="questions")
    notebook_config: Mapped["NotebookQuestionConfig | None"] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        uselist=False,
    )
    python_code_config: Mapped["PythonCodeQuestionConfig | None"] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        uselist=False,
    )
    short_answer_config: Mapped["ShortAnswerQuestionConfig | None"] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        uselist=False,
    )
    file_question_config: Mapped["FileQuestionConfig | None"] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        uselist=False,
    )
    submissions: Mapped[list["Submission"]] = relationship(back_populates="question")
    runtime_image: Mapped[RuntimeImage | None] = relationship(
        back_populates="questions_using_as_override",
        foreign_keys=[runtime_image_id],
    )
    llm_config: Mapped[LLMConfig | None] = relationship(
        back_populates="questions_using_as_override",
        foreign_keys=[llm_config_id],
    )
    final_grade_snapshots: Mapped[list["FinalGradeSnapshot"]] = relationship(back_populates="question")


Index("ix_questions_assignment_order", Question.assignment_id, Question.order_index)


class NotebookQuestionConfig(Base):
    __tablename__ = "notebook_question_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    time_limit_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, default=1024, nullable=False)
    cpu_limit: Mapped[str] = mapped_column(String(16), default="1", nullable=False)
    allow_network: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    visible_tests_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    hidden_tests_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_weight: Mapped[Numeric] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    visible_weight: Mapped[Numeric] = mapped_column(Numeric(10, 2), default=100, nullable=False)
    hidden_weight: Mapped[Numeric] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    llm_score_weight: Mapped[Numeric] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    llm_scoring_rubric: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_feedback_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    question: Mapped[Question] = relationship(back_populates="notebook_config")


class PythonCodeQuestionConfig(Base):
    __tablename__ = "python_code_question_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    input_spec: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_spec: Mapped[str | None] = mapped_column(Text, nullable=True)
    visible_tests_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    hidden_tests_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_libraries_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_limit_seconds: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    cpu_limit: Mapped[str] = mapped_column(String(16), default="1", nullable=False)
    allow_network: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    question: Mapped[Question] = relationship(back_populates="python_code_config")

    def visible_tests(self) -> list[dict]:
        try:
            payload = json.loads(self.visible_tests_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [_normalize_python_test_case(item, index) for index, item in enumerate(payload, start=1) if isinstance(item, dict)]

    def hidden_tests(self) -> list[dict]:
        try:
            payload = json.loads(self.hidden_tests_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [_normalize_python_test_case(item, index) for index, item in enumerate(payload, start=1) if isinstance(item, dict)]


class ShortAnswerQuestionConfig(Base):
    __tablename__ = "short_answer_question_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    min_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rubric_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_suggestion_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    teacher_confirmation_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    question: Mapped[Question] = relationship(back_populates="short_answer_config")


class FileQuestionConfig(Base):
    __tablename__ = "file_question_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    accepted_extensions: Mapped[str] = mapped_column(String(255), nullable=False)
    reference_answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    rubric_text: Mapped[str] = mapped_column(Text, nullable=False)
    llm_suggestion_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    teacher_confirmation_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notebook_outputs_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    question: Mapped[Question] = relationship(back_populates="file_question_config")


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    submission_type: Mapped[QuestionType] = mapped_column(
        Enum(QuestionType, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=SubmissionStatus.SUBMITTED,
        index=True,
    )
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notebook_id: Mapped[int | None] = mapped_column(ForeignKey("notebooks.id", ondelete="SET NULL"), nullable=True)
    stored_file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    queued_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    counts_toward_limit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_effective_submission: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    course: Mapped[Course] = relationship(back_populates="submissions")
    assignment: Mapped[Assignment] = relationship(back_populates="submissions")
    question: Mapped[Question] = relationship(back_populates="submissions")
    user: Mapped[User] = relationship(back_populates="submissions")
    notebook: Mapped[Notebook | None] = relationship(back_populates="submissions")
    evaluation_tasks: Mapped[list["EvaluationTask"]] = relationship(back_populates="submission", cascade="all, delete-orphan")
    evaluation_results: Mapped[list["EvaluationResult"]] = relationship(back_populates="submission", cascade="all, delete-orphan")
    feedback_items: Mapped[list["Feedback"]] = relationship(back_populates="submission", cascade="all, delete-orphan")
    grade_snapshots_using_submission: Mapped[list["FinalGradeSnapshot"]] = relationship(
        back_populates="effective_submission",
        foreign_keys="FinalGradeSnapshot.effective_submission_id",
    )


Index("ix_submissions_question_user_submitted", Submission.question_id, Submission.user_id, Submission.submitted_at)


class EvaluationTask(Base):
    __tablename__ = "evaluation_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    task_type: Mapped[EvaluationTaskType] = mapped_column(
        Enum(EvaluationTaskType, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=EvaluationTaskType.NOTEBOOK_EVALUATION,
    )
    backend_type: Mapped[str] = mapped_column(String(32), default="rq", nullable=False)
    backend_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[EvaluationTaskStatus] = mapped_column(
        Enum(EvaluationTaskStatus, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=EvaluationTaskStatus.QUEUED,
        index=True,
    )
    runtime_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtime_images.id", ondelete="SET NULL"),
        nullable=True,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    submission: Mapped[Submission] = relationship(back_populates="evaluation_tasks")
    runtime_image: Mapped[RuntimeImage | None] = relationship(back_populates="evaluation_tasks")
    evaluation_results: Mapped[list["EvaluationResult"]] = relationship(back_populates="evaluation_task")


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    evaluation_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("evaluation_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    visible_score: Mapped[Numeric | None] = mapped_column(Numeric(10, 2), nullable=True)
    hidden_score: Mapped[Numeric | None] = mapped_column(Numeric(10, 2), nullable=True)
    auto_score: Mapped[Numeric | None] = mapped_column(Numeric(10, 2), nullable=True)
    final_score: Mapped[Numeric | None] = mapped_column(Numeric(10, 2), nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stdout_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stderr_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    rendered_html_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    executed_notebook_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    submission: Mapped[Submission] = relationship(back_populates="evaluation_results")
    evaluation_task: Mapped[EvaluationTask | None] = relationship(back_populates="evaluation_results")
    feedback_items: Mapped[list["Feedback"]] = relationship(back_populates="evaluation_result")


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    evaluation_result_id: Mapped[int | None] = mapped_column(
        ForeignKey("evaluation_results.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source: Mapped[FeedbackSource] = mapped_column(
        Enum(FeedbackSource, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        index=True,
    )
    score_suggestion: Mapped[Numeric | None] = mapped_column(Numeric(10, 2), nullable=True)
    comment_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    submission: Mapped[Submission] = relationship(back_populates="feedback_items")
    evaluation_result: Mapped[EvaluationResult | None] = relationship(back_populates="feedback_items")
    author: Mapped[User | None] = relationship(back_populates="created_feedback", foreign_keys=[created_by])


class FinalGradeSnapshot(Base):
    __tablename__ = "final_grade_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True)
    effective_submission_id: Mapped[int | None] = mapped_column(
        ForeignKey("submissions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    grading_rule_applied: Mapped[ScoringRule] = mapped_column(
        Enum(ScoringRule, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
    )
    score: Mapped[Numeric | None] = mapped_column(Numeric(10, 2), nullable=True)
    feedback_source: Mapped[FeedbackSource | None] = mapped_column(
        Enum(FeedbackSource, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=True,
    )
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    student: Mapped[User] = relationship(
        back_populates="final_grade_snapshots",
        foreign_keys=[student_id],
    )
    assignment: Mapped[Assignment] = relationship(back_populates="final_grade_snapshots")
    question: Mapped[Question] = relationship(back_populates="final_grade_snapshots")
    effective_submission: Mapped[Submission | None] = relationship(
        back_populates="grade_snapshots_using_submission",
        foreign_keys=[effective_submission_id],
    )


Index("ix_final_grade_snapshot_unique", FinalGradeSnapshot.student_id, FinalGradeSnapshot.question_id, unique=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    notebook_id: Mapped[int] = mapped_column(ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=JobStatus.QUEUED,
        index=True,
    )
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="jobs")
    notebook: Mapped[Notebook] = relationship(back_populates="jobs")
    output: Mapped["JobOutput | None"] = relationship(back_populates="job", cascade="all, delete-orphan", uselist=False)


Index("ix_jobs_user_status_created", Job.user_id, Job.status, Job.created_at)


class JobOutput(Base):
    __tablename__ = "job_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    executed_notebook_path: Mapped[str] = mapped_column(String(512), nullable=False)
    html_path: Mapped[str] = mapped_column(String(512), nullable=False)
    stdout_path: Mapped[str] = mapped_column(String(512), nullable=False)
    stderr_path: Mapped[str] = mapped_column(String(512), nullable=False)

    job: Mapped[Job] = relationship(back_populates="output")
