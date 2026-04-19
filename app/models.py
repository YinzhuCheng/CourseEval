import json

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import (
    AccountRole,
    AssignmentStatus,
    CodeLanguage,
    CodeSubmissionMode,
    CourseRole,
    CourseStatus,
    DiscussionTopicKind,
    LLMResponseLanguage,
    EvaluationTaskStatus,
    EvaluationTaskType,
    FeedbackSource,
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


def _normalize_code_test_case(item: dict, index: int) -> dict:
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
        "points": points,
    }


def _normalize_code_language(value: str | CodeLanguage | None) -> str:
    raw = value.value if isinstance(value, CodeLanguage) else str(value or "").strip().lower()
    return raw if raw in {item.value for item in CodeLanguage} else CodeLanguage.PYTHON.value


def _normalize_code_language_list(raw: str | None) -> list[CodeLanguage]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        payload = []
    if not isinstance(payload, list):
        payload = []
    result: list[CodeLanguage] = []
    for item in payload:
        try:
            language = CodeLanguage(str(item).strip().lower())
        except ValueError:
            continue
        if language not in result:
            result.append(language)
    return result or [CodeLanguage.PYTHON]


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
    email_verification_last_send_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_reset_token: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    password_reset_sent_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    llm_daily_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    avatar_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    avatar_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    storage_quota_override_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    course_memberships: Mapped[list["CourseMember"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="CourseMember.user_id",
    )
    discussion_posts: Mapped[list["DiscussionPost"]] = relationship(
        back_populates="author",
        cascade="all, delete-orphan",
        foreign_keys="DiscussionPost.author_id",
    )
    discussion_mutes_received: Mapped[list["CourseDiscussionMute"]] = relationship(
        back_populates="user",
        foreign_keys="CourseDiscussionMute.user_id",
        cascade="all, delete-orphan",
    )
    created_courses: Mapped[list["Course"]] = relationship(
        back_populates="creator",
        foreign_keys="Course.created_by",
    )
    created_free_discussion_topics: Mapped[list["FreeDiscussionTopic"]] = relationship(
        back_populates="creator",
        foreign_keys="FreeDiscussionTopic.created_by",
    )
    created_runtime_images: Mapped[list["RuntimeImage"]] = relationship(
        back_populates="creator",
        foreign_keys="RuntimeImage.created_by",
    )
    created_llm_configs: Mapped[list["LLMConfig"]] = relationship(
        back_populates="creator",
        foreign_keys="LLMConfig.created_by",
    )
    created_llm_config_members: Mapped[list["LLMConfigMember"]] = relationship(
        back_populates="creator",
        foreign_keys="LLMConfigMember.created_by",
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
    llm_token_daily_rows: Mapped[list["UserLlmTokenDaily"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    email_delivery_logs: Mapped[list["EmailDeliveryLog"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    stored_objects: Mapped[list["UserStoredObject"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="UserStoredObject.user_id",
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


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    join_code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_open_community: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_hidden_from_course_lists: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    use_global_llm_default: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    llm_response_language: Mapped[str] = mapped_column(
        String(8), nullable=False, default=LLMResponseLanguage.AUTO.value
    )
    discussion_ai_question_llm_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    discussion_ai_material_llm_config_id: Mapped[int | None] = mapped_column(
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
    materials: Mapped[list["CourseMaterial"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    free_discussion_topics: Mapped[list["FreeDiscussionTopic"]] = relationship(
        back_populates="course",
        cascade="all, delete-orphan",
    )
    discussion_mutes: Mapped[list["CourseDiscussionMute"]] = relationship(
        back_populates="course",
        cascade="all, delete-orphan",
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


class PlatformLlmTokenPolicy(Base):
    """Singleton row id=1: default daily LLM token budget per user (Beijing calendar day)."""

    __tablename__ = "platform_llm_token_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    default_user_daily_llm_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=100000)
    discussion_posts_page_size: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    discussion_ai_default_llm_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    discussion_ai_question_llm_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    discussion_ai_material_llm_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    default_student_storage_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=100 * 1024 * 1024)
    default_teacher_storage_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=1024 * 1024 * 1024)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserLlmTokenDaily(Base):
    __tablename__ = "user_llm_token_daily"
    __table_args__ = (Index("ix_user_llm_token_daily_user_date", "user_id", "usage_date", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False)
    consumed_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    user: Mapped["User"] = relationship(back_populates="llm_token_daily_rows")


class EmailDeliveryLog(Base):
    __tablename__ = "email_delivery_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    user: Mapped[User | None] = relationship(back_populates="email_delivery_logs")


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
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    queue_concurrency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_llm_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    llm_retry_initial_seconds: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
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
    members: Mapped[list["LLMConfigMember"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="LLMConfigMember.priority_order",
    )


class LLMConfigMember(Base):
    __tablename__ = "llm_config_members"
    __table_args__ = (Index("ix_llm_config_members_group_priority", "group_id", "priority_order", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("llm_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    priority_order: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
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
    max_llm_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    llm_retry_initial_seconds: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
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

    group: Mapped[LLMConfig] = relationship(back_populates="members", foreign_keys=[group_id])
    creator: Mapped[User | None] = relationship(
        back_populates="created_llm_config_members",
        foreign_keys=[created_by],
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
    code_config: Mapped["CodeQuestionConfig | None"] = relationship(
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
    discussion_topic: Mapped["DiscussionTopic | None"] = relationship(
        back_populates="question",
        uselist=False,
        cascade="all, delete-orphan",
    )
    versions: Mapped[list["QuestionVersion"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="QuestionVersion.version_number",
    )
    current_question_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    current_version: Mapped["QuestionVersion | None"] = relationship(
        "QuestionVersion",
        primaryjoin="Question.current_question_version_id==QuestionVersion.id",
        foreign_keys="QuestionVersion.id",
        post_update=True,
        uselist=False,
    )

Index("ix_questions_assignment_order", Question.assignment_id, Question.order_index)


class QuestionVersion(Base):
    __tablename__ = "question_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    question: Mapped["Question"] = relationship(
        back_populates="versions",
        foreign_keys=[question_id],
    )


Index("ix_question_versions_question_version", QuestionVersion.question_id, QuestionVersion.version_number, unique=True)


class CodeQuestionConfig(Base):
    __tablename__ = "code_question_configs"

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
    allowed_languages_json: Mapped[str] = mapped_column(Text, nullable=False, default='["python"]')
    reference_solution_python: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reference_solution_c: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reference_solution_cpp: Mapped[str] = mapped_column(Text, nullable=False, default="")
    time_limit_seconds: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    cpu_limit: Mapped[str] = mapped_column(String(16), default="1", nullable=False)
    allow_network: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    question: Mapped[Question] = relationship(back_populates="code_config")

    def allowed_languages(self) -> list[CodeLanguage]:
        return _normalize_code_language_list(self.allowed_languages_json)

    def allowed_language_values(self) -> list[str]:
        return [language.value for language in self.allowed_languages()]

    def allows_language(self, language: CodeLanguage | str) -> bool:
        normalized = CodeLanguage(_normalize_code_language(language))
        return normalized in self.allowed_languages()

    def visible_tests(self) -> list[dict]:
        try:
            payload = json.loads(self.visible_tests_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [_normalize_code_test_case(item, index) for index, item in enumerate(payload, start=1) if isinstance(item, dict)]

    def hidden_tests(self) -> list[dict]:
        try:
            payload = json.loads(self.hidden_tests_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [_normalize_code_test_case(item, index) for index, item in enumerate(payload, start=1) if isinstance(item, dict)]

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
    teacher_confirmation_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    reference_answer_file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    rubric_text: Mapped[str] = mapped_column(Text, nullable=False)
    llm_suggestion_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    teacher_confirmation_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    stored_file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_language: Mapped[CodeLanguage | None] = mapped_column(
        Enum(CodeLanguage, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=True,
        index=True,
    )
    code_submission_mode: Mapped[CodeSubmissionMode | None] = mapped_column(
        Enum(CodeSubmissionMode, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=True,
    )
    submitted_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    queued_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    counts_toward_limit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_effective_submission: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    stored_file_purged_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stored_file_purge_actor: Mapped[str | None] = mapped_column(String(32), nullable=True)
    question_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("question_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    course: Mapped[Course] = relationship(back_populates="submissions")
    assignment: Mapped[Assignment] = relationship(back_populates="submissions")
    question: Mapped[Question] = relationship(back_populates="submissions")
    user: Mapped[User] = relationship(back_populates="submissions")
    evaluation_tasks: Mapped[list["EvaluationTask"]] = relationship(back_populates="submission", cascade="all, delete-orphan")
    evaluation_results: Mapped[list["EvaluationResult"]] = relationship(back_populates="submission", cascade="all, delete-orphan")
    feedback_items: Mapped[list["Feedback"]] = relationship(back_populates="submission", cascade="all, delete-orphan")
    grade_snapshots_using_submission: Mapped[list["FinalGradeSnapshot"]] = relationship(
        back_populates="effective_submission",
        foreign_keys="FinalGradeSnapshot.effective_submission_id",
    )
    question_version: Mapped["QuestionVersion | None"] = relationship(foreign_keys=[question_version_id])


Index("ix_submissions_question_user_submitted", Submission.question_id, Submission.user_id, Submission.submitted_at)


class EvaluationTask(Base):
    __tablename__ = "evaluation_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    task_type: Mapped[EvaluationTaskType] = mapped_column(
        Enum(EvaluationTaskType, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        default=EvaluationTaskType.CODE_EVALUATION,
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
    question_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("question_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    use_historical_highest: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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
    question_version: Mapped["QuestionVersion | None"] = relationship(foreign_keys=[question_version_id])


Index("ix_final_grade_snapshot_unique", FinalGradeSnapshot.student_id, FinalGradeSnapshot.question_id, unique=True)


class CourseMaterial(Base):
    __tablename__ = "course_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    course: Mapped[Course] = relationship(back_populates="materials")
    creator: Mapped[User | None] = relationship(foreign_keys=[created_by])
    discussion_topic: Mapped["DiscussionTopic | None"] = relationship(
        back_populates="course_material",
        uselist=False,
        cascade="all, delete-orphan",
    )


class FreeDiscussionTopic(Base):
    """A user-created topic card in the platform open discussion space (not a normal course)."""

    __tablename__ = "free_discussion_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    course: Mapped["Course"] = relationship()
    creator: Mapped["User"] = relationship(foreign_keys=[created_by])
    discussion_topic: Mapped["DiscussionTopic | None"] = relationship(
        back_populates="free_discussion_topic",
        uselist=False,
        cascade="all, delete-orphan",
    )


class DiscussionTopic(Base):
    __tablename__ = "discussion_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[DiscussionTopicKind] = mapped_column(
        Enum(DiscussionTopicKind, native_enum=False, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        index=True,
    )
    course_material_id: Mapped[int | None] = mapped_column(
        ForeignKey("course_materials.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )
    question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), nullable=True, unique=True)
    free_discussion_topic_id: Mapped[int | None] = mapped_column(
        ForeignKey("free_discussion_topics.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    course: Mapped[Course] = relationship()
    course_material: Mapped["CourseMaterial | None"] = relationship(back_populates="discussion_topic", foreign_keys=[course_material_id])
    question: Mapped["Question | None"] = relationship(back_populates="discussion_topic", foreign_keys=[question_id])
    free_discussion_topic: Mapped["FreeDiscussionTopic | None"] = relationship(
        back_populates="discussion_topic",
        foreign_keys=[free_discussion_topic_id],
    )
    posts: Mapped[list["DiscussionPost"]] = relationship(
        back_populates="topic",
        cascade="all, delete-orphan",
        order_by="DiscussionPost.created_at.asc()",
    )


Index("ix_discussion_topics_course_kind", DiscussionTopic.course_id, DiscussionTopic.kind)


class DiscussionPost(Base):
    __tablename__ = "discussion_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("discussion_topics.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_post_id: Mapped[int | None] = mapped_column(ForeignKey("discussion_posts.id", ondelete="CASCADE"), nullable=True, index=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    deleted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    deleted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    topic: Mapped[DiscussionTopic] = relationship(back_populates="posts")
    author: Mapped[User] = relationship(
        back_populates="discussion_posts",
        foreign_keys=[author_id],
    )
    deleted_by: Mapped["User | None"] = relationship(foreign_keys=[deleted_by_id])
    parent: Mapped["DiscussionPost | None"] = relationship(remote_side="DiscussionPost.id", back_populates="replies")
    replies: Mapped[list["DiscussionPost"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    attachments: Mapped[list["DiscussionPostAttachment"]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="DiscussionPostAttachment.id.asc()",
    )


Index("ix_discussion_posts_topic_created", DiscussionPost.topic_id, DiscussionPost.created_at)


class DiscussionPostAttachment(Base):
    __tablename__ = "discussion_post_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("discussion_posts.id", ondelete="CASCADE"), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    post: Mapped[DiscussionPost] = relationship(back_populates="attachments")


class CourseDiscussionMute(Base):
    __tablename__ = "course_discussion_mutes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    muted_until: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    course: Mapped[Course] = relationship(back_populates="discussion_mutes", foreign_keys=[course_id])
    user: Mapped[User] = relationship(back_populates="discussion_mutes_received", foreign_keys=[user_id])
    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])


Index("ix_course_discussion_mutes_course_user", CourseDiscussionMute.course_id, CourseDiscussionMute.user_id, unique=True)


class DiscussionModerationLog(Base):
    __tablename__ = "discussion_moderation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserStoredObject(Base):
    """Tracks per-user storage usage for quota enforcement and asset deletion."""

    __tablename__ = "user_stored_objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    deleted_by_actor: Mapped[str | None] = mapped_column(String(32), nullable=True)

    user: Mapped["User"] = relationship(back_populates="stored_objects")


Index("ix_user_stored_objects_user_deleted", UserStoredObject.user_id, UserStoredObject.deleted_at)
