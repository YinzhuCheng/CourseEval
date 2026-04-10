import secrets
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.constants import (
    AccountRole,
    AssignmentStatus,
    CourseRole,
    CourseStatus,
    FeedbackSource,
    MembershipStatus,
    QuestionType,
    ScoringRule,
)
from app.db import utcnow
from app.models import (
    Assignment,
    Course,
    CourseMember,
    Feedback,
    FinalGradeSnapshot,
    LLMConfig,
    NotebookQuestionConfig,
    Question,
    RuntimeImage,
    ShortAnswerQuestionConfig,
    Submission,
    User,
)


STAFF_COURSE_ROLES = (CourseRole.TEACHER, CourseRole.TA)


def generate_join_code() -> str:
    return secrets.token_hex(3).upper()


def list_courses_for_student(db: Session, user_id: int) -> list[Course]:
    statement = (
        select(Course)
        .join(CourseMember, CourseMember.course_id == Course.id)
        .where(
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
        .order_by(Course.title.asc())
    )
    return list(db.scalars(statement).unique())


def get_course_for_student(db: Session, course_id: int, user_id: int) -> Course | None:
    statement = (
        select(Course)
        .options(
            joinedload(Course.assignments).joinedload(Assignment.questions),
            joinedload(Course.members).joinedload(CourseMember.user),
        )
        .join(CourseMember, CourseMember.course_id == Course.id)
        .where(
            Course.id == course_id,
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_assignment_for_student(db: Session, assignment_id: int, user_id: int) -> Assignment | None:
    statement = (
        select(Assignment)
        .options(
            joinedload(Assignment.course),
            joinedload(Assignment.questions).joinedload(Question.notebook_config),
            joinedload(Assignment.questions).joinedload(Question.short_answer_config),
        )
        .join(CourseMember, CourseMember.course_id == Assignment.course_id)
        .where(
            Assignment.id == assignment_id,
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_question_for_student(db: Session, question_id: int, user_id: int) -> Question | None:
    statement = (
        select(Question)
        .options(
            joinedload(Question.assignment).joinedload(Assignment.course),
            joinedload(Question.notebook_config),
            joinedload(Question.short_answer_config),
        )
        .join(Assignment, Question.assignment_id == Assignment.id)
        .join(CourseMember, CourseMember.course_id == Assignment.course_id)
        .where(
            Question.id == question_id,
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_course_for_teacher(db: Session, course_id: int, teacher_user_id: int) -> Course | None:
    statement = (
        select(Course)
        .options(
            joinedload(Course.assignments).joinedload(Assignment.questions),
            joinedload(Course.members).joinedload(CourseMember.user),
        )
        .join(CourseMember, CourseMember.course_id == Course.id)
        .where(
            Course.id == course_id,
            CourseMember.user_id == teacher_user_id,
            CourseMember.role == CourseRole.TEACHER,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_question_for_teacher(db: Session, question_id: int, teacher_user_id: int) -> Question | None:
    statement = (
        select(Question)
        .options(
            joinedload(Question.assignment).joinedload(Assignment.course),
            joinedload(Question.notebook_config),
            joinedload(Question.short_answer_config),
        )
        .join(Assignment, Question.assignment_id == Assignment.id)
        .join(CourseMember, CourseMember.course_id == Assignment.course_id)
        .where(
            Question.id == question_id,
            CourseMember.user_id == teacher_user_id,
            CourseMember.role == CourseRole.TEACHER,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def list_courses_for_user(db: Session, user: User) -> list[Course]:
    if user.platform_role.value == "admin":
        statement = select(Course).order_by(Course.title.asc())
    else:
        statement = (
            select(Course)
            .join(CourseMember, CourseMember.course_id == Course.id)
            .where(CourseMember.user_id == user.id, CourseMember.status == MembershipStatus.ACTIVE)
            .order_by(Course.title.asc())
        )
    return list(db.scalars(statement).unique())


def list_courses_for_student(db: Session, user_id: int) -> list[Course]:
    statement = (
        select(Course)
        .join(CourseMember, CourseMember.course_id == Course.id)
        .where(
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
        .order_by(Course.title.asc())
    )
    return list(db.scalars(statement).unique())


def get_course_for_student(db: Session, course_id: int, user_id: int) -> Course | None:
    statement = (
        select(Course)
        .options(
            joinedload(Course.assignments).joinedload(Assignment.questions),
            joinedload(Course.members).joinedload(CourseMember.user),
        )
        .join(CourseMember, CourseMember.course_id == Course.id)
        .where(
            Course.id == course_id,
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_assignment_for_student(db: Session, assignment_id: int, user_id: int) -> Assignment | None:
    statement = (
        select(Assignment)
        .options(
            joinedload(Assignment.course),
            joinedload(Assignment.questions).joinedload(Question.notebook_config),
            joinedload(Assignment.questions).joinedload(Question.short_answer_config),
        )
        .join(CourseMember, CourseMember.course_id == Assignment.course_id)
        .where(
            Assignment.id == assignment_id,
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_course_for_staff(db: Session, course_id: int, user_id: int) -> Course | None:
    statement = (
        select(Course)
        .options(
            joinedload(Course.assignments).joinedload(Assignment.questions),
            joinedload(Course.members).joinedload(CourseMember.user),
        )
        .join(CourseMember, CourseMember.course_id == Course.id)
        .where(
            Course.id == course_id,
            CourseMember.user_id == user_id,
            CourseMember.role.in_(STAFF_COURSE_ROLES),
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_assignment_for_staff(db: Session, assignment_id: int, user_id: int) -> Assignment | None:
    statement = (
        select(Assignment)
        .options(
            joinedload(Assignment.course),
            joinedload(Assignment.questions).joinedload(Question.notebook_config),
            joinedload(Assignment.questions).joinedload(Question.short_answer_config),
        )
        .join(CourseMember, CourseMember.course_id == Assignment.course_id)
        .where(
            Assignment.id == assignment_id,
            CourseMember.user_id == user_id,
            CourseMember.role.in_(STAFF_COURSE_ROLES),
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_course_for_teacher(db: Session, course_id: int, user_id: int) -> Course | None:
    statement = (
        select(Course)
        .options(
            joinedload(Course.assignments).joinedload(Assignment.questions),
            joinedload(Course.members).joinedload(CourseMember.user),
        )
        .join(CourseMember, CourseMember.course_id == Course.id)
        .where(
            Course.id == course_id,
            CourseMember.user_id == user_id,
            CourseMember.role == CourseRole.TEACHER,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_question_for_staff(db: Session, question_id: int, user_id: int) -> Question | None:
    statement = (
        select(Question)
        .options(
            joinedload(Question.assignment).joinedload(Assignment.course),
            joinedload(Question.notebook_config),
            joinedload(Question.short_answer_config),
        )
        .join(Assignment, Assignment.id == Question.assignment_id)
        .join(CourseMember, CourseMember.course_id == Assignment.course_id)
        .where(
            Question.id == question_id,
            CourseMember.user_id == user_id,
            CourseMember.role.in_(STAFF_COURSE_ROLES),
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_question_for_teacher(db: Session, question_id: int, user_id: int) -> Question | None:
    statement = (
        select(Question)
        .options(
            joinedload(Question.assignment).joinedload(Assignment.course),
            joinedload(Question.notebook_config),
            joinedload(Question.short_answer_config),
        )
        .join(Assignment, Assignment.id == Question.assignment_id)
        .join(CourseMember, CourseMember.course_id == Assignment.course_id)
        .where(
            Question.id == question_id,
            CourseMember.user_id == user_id,
            CourseMember.role == CourseRole.TEACHER,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def list_all_courses(db: Session) -> list[Course]:
    statement = select(Course).order_by(Course.created_at.desc())
    return list(db.scalars(statement).unique())


def get_course(db: Session, course_id: int) -> Course | None:
    statement = (
        select(Course)
        .options(
            joinedload(Course.members).joinedload(CourseMember.user),
            joinedload(Course.assignments).joinedload(Assignment.questions),
            joinedload(Course.default_runtime_image),
            joinedload(Course.default_llm_config),
        )
        .where(Course.id == course_id)
    )
    return db.scalar(statement)


def create_course(
    db: Session,
    *,
    code: str,
    title: str,
    description: str,
    creator: User,
    teacher_user_ids: list[int],
    student_user_ids: list[int],
) -> Course:
    course = Course(
        code=code.strip().upper(),
        join_code=generate_join_code(),
        title=title.strip(),
        description=description.strip() or None,
        status=CourseStatus.ACTIVE,
        created_by=creator.id,
        updated_at=utcnow(),
    )
    db.add(course)
    db.flush()

    member_user_ids = {creator.id, *teacher_user_ids, *student_user_ids}
    users = {
        user.id: user
        for user in db.scalars(select(User).where(User.id.in_(member_user_ids))).all()
    }

    def add_member(user_id: int, role: CourseRole) -> None:
        if user_id not in users:
            return
        db.add(
            CourseMember(
                course_id=course.id,
                user_id=user_id,
                role=role,
                status=MembershipStatus.ACTIVE,
            )
        )

    add_member(creator.id, CourseRole.TEACHER)
    for teacher_id in teacher_user_ids:
        if teacher_id != creator.id:
            add_member(teacher_id, CourseRole.TEACHER)
    for student_id in student_user_ids:
        if student_id != creator.id and student_id not in teacher_user_ids:
            add_member(student_id, CourseRole.STUDENT)

    db.commit()
    return get_course(db, course.id)  # type: ignore[return-value]


def join_course_by_code(db: Session, *, user: User, join_code: str) -> Course:
    normalized = join_code.strip().upper()
    course = db.scalar(select(Course).where(Course.join_code == normalized))
    if course is None:
        raise ValueError("Course join code is invalid.")

    membership = db.scalar(
        select(CourseMember).where(
            CourseMember.course_id == course.id,
            CourseMember.user_id == user.id,
        )
    )
    if membership is None:
        membership = CourseMember(
            course_id=course.id,
            user_id=user.id,
            role=CourseRole.STUDENT,
            status=MembershipStatus.ACTIVE,
        )
        db.add(membership)
    else:
        membership.role = CourseRole.STUDENT
        membership.status = MembershipStatus.ACTIVE
    db.commit()
    return course


def create_assignment(
    db: Session,
    *,
    course: Course,
    title: str,
    description: str,
    status: AssignmentStatus,
    open_at,
    due_at,
    close_at,
    allow_late: bool,
    default_scoring_rule: ScoringRule,
    submission_limit_mode,
    submission_limit_value: int | None,
    runtime_image_id: int | None,
    llm_config_id: int | None,
) -> Assignment:
    assignment = Assignment(
        course_id=course.id,
        title=title.strip(),
        description=description.strip() or None,
        status=status,
        published_at=utcnow() if status == AssignmentStatus.PUBLISHED else None,
        open_at=open_at,
        due_at=due_at,
        close_at=close_at,
        allow_late=allow_late,
        default_scoring_rule=default_scoring_rule,
        submission_limit_mode=submission_limit_mode,
        submission_limit_value=submission_limit_value,
        runtime_image_id=runtime_image_id,
        llm_config_id=llm_config_id,
        updated_at=utcnow(),
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return assignment


def get_assignment(db: Session, assignment_id: int) -> Assignment | None:
    statement = (
        select(Assignment)
        .options(
            joinedload(Assignment.course),
            joinedload(Assignment.questions).joinedload(Question.notebook_config),
            joinedload(Assignment.questions).joinedload(Question.short_answer_config),
            joinedload(Assignment.runtime_image),
            joinedload(Assignment.llm_config),
        )
        .where(Assignment.id == assignment_id)
    )
    return db.scalar(statement)


def create_question(
    db: Session,
    *,
    assignment: Assignment,
    order_index: int,
    title: str,
    description: str,
    question_type: QuestionType,
    max_score: Decimal,
    scoring_rule_override: ScoringRule | None,
    runtime_image_id: int | None,
    llm_config_id: int | None,
    notebook_config_payload: dict | None,
    short_answer_payload: dict | None,
) -> Question:
    question = Question(
        assignment_id=assignment.id,
        order_index=order_index,
        title=title.strip(),
        description=description.strip() or None,
        question_type=question_type,
        max_score=max_score,
        scoring_rule_override=scoring_rule_override,
        runtime_image_id=runtime_image_id,
        llm_config_id=llm_config_id,
        updated_at=utcnow(),
    )
    db.add(question)
    db.flush()

    if question_type == QuestionType.NOTEBOOK:
        payload = notebook_config_payload or {}
        db.add(
            NotebookQuestionConfig(
                question_id=question.id,
                time_limit_seconds=payload.get("time_limit_seconds", 300),
                memory_limit_mb=payload.get("memory_limit_mb", 1024),
                cpu_limit=payload.get("cpu_limit", "1"),
                allow_network=payload.get("allow_network", False),
                visible_tests_source=payload.get("visible_tests_source") or None,
                hidden_tests_source=payload.get("hidden_tests_source") or None,
                execution_weight=payload.get("execution_weight", Decimal("0")),
                visible_weight=payload.get("visible_weight", Decimal("100")),
                hidden_weight=payload.get("hidden_weight", Decimal("0")),
                llm_feedback_enabled=payload.get("llm_feedback_enabled", False),
                updated_at=utcnow(),
            )
        )
    else:
        payload = short_answer_payload or {}
        db.add(
            ShortAnswerQuestionConfig(
                question_id=question.id,
                min_length=payload.get("min_length"),
                max_length=payload.get("max_length"),
                rubric_text=payload.get("rubric_text") or None,
                llm_suggestion_enabled=payload.get("llm_suggestion_enabled", False),
                teacher_confirmation_required=payload.get("teacher_confirmation_required", True),
                updated_at=utcnow(),
            )
        )

    db.commit()
    statement = (
        select(Question)
        .options(joinedload(Question.notebook_config), joinedload(Question.short_answer_config))
        .where(Question.id == question.id)
    )
    return db.scalar(statement)  # type: ignore[return-value]


def list_users(db: Session) -> list[User]:
    statement = select(User).order_by(User.username.asc())
    return list(db.scalars(statement).unique())


def list_runtime_images(db: Session, course_id: int | None = None) -> list[RuntimeImage]:
    statement = select(RuntimeImage).order_by(RuntimeImage.created_at.desc())
    if course_id is not None:
        statement = statement.where(
            (RuntimeImage.scope == "platform") | (RuntimeImage.course_id == course_id)
        )
    return list(db.scalars(statement).unique())


def list_llm_configs(db: Session, course_id: int | None = None) -> list[LLMConfig]:
    statement = select(LLMConfig).order_by(LLMConfig.created_at.desc())
    if course_id is not None:
        statement = statement.where((LLMConfig.scope == "platform") | (LLMConfig.course_id == course_id))
    return list(db.scalars(statement).unique())


def create_runtime_image(
    db: Session,
    *,
    scope,
    course_id: int | None,
    name: str,
    image_tag: str,
    python_version: str,
    package_summary: str,
    network_enabled: bool,
    timeout_seconds: int,
    memory_limit_mb: int,
    cpu_limit: str,
    creator_id: int | None,
) -> RuntimeImage:
    runtime_image = RuntimeImage(
        scope=scope,
        course_id=course_id,
        name=name.strip(),
        image_tag=image_tag.strip(),
        python_version=python_version.strip() or None,
        package_summary=package_summary.strip() or None,
        network_enabled=network_enabled,
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
        cpu_limit=cpu_limit.strip() or "1",
        created_by=creator_id,
        updated_at=utcnow(),
    )
    db.add(runtime_image)
    db.commit()
    db.refresh(runtime_image)
    return runtime_image


def create_llm_config(
    db: Session,
    *,
    scope,
    course_id: int | None,
    name: str,
    provider_type,
    base_url: str,
    api_key: str,
    model_name: str,
    timeout_seconds: int,
    max_tokens: int,
    temperature: str,
    enabled: bool,
    creator_id: int | None,
) -> LLMConfig:
    llm_config = LLMConfig(
        scope=scope,
        course_id=course_id,
        name=name.strip(),
        provider_type=provider_type,
        base_url=base_url.strip() or None,
        api_key=api_key.strip() or None,
        model_name=model_name.strip(),
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        temperature=temperature.strip() or "0.2",
        enabled=enabled,
        created_by=creator_id,
        updated_at=utcnow(),
    )
    db.add(llm_config)
    db.commit()
    db.refresh(llm_config)
    return llm_config


def summarize_course_grades(db: Session, assignment_id: int) -> list[dict]:
    assignment = get_assignment(db, assignment_id)
    if assignment is None:
        return []

    statement = (
        select(
            User.id,
            User.username,
            func.coalesce(func.sum(FinalGradeSnapshot.score), 0),
        )
        .select_from(CourseMember)
        .join(User, User.id == CourseMember.user_id)
        .outerjoin(
            FinalGradeSnapshot,
            (FinalGradeSnapshot.student_id == User.id)
            & (FinalGradeSnapshot.assignment_id == assignment_id),
        )
        .where(
            CourseMember.course_id == assignment.course_id,
            CourseMember.role == CourseRole.STUDENT,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
        .group_by(User.id, User.username)
        .order_by(User.username.asc())
    )
    rows = db.execute(statement).all()
    return [{"student_id": row[0], "username": row[1], "score": row[2]} for row in rows]


def resolve_submission_score(
    submission: Submission,
    latest_feedback: Feedback | None,
) -> tuple[Decimal | None, FeedbackSource | None]:
    if latest_feedback and latest_feedback.source == FeedbackSource.TEACHER:
        return latest_feedback.score_suggestion, FeedbackSource.TEACHER

    teacher_feedback = next((item for item in submission.feedback_items if item.source == FeedbackSource.TEACHER), None)
    if teacher_feedback is not None:
        return teacher_feedback.score_suggestion, FeedbackSource.TEACHER

    llm_feedback = next((item for item in submission.feedback_items if item.source == FeedbackSource.LLM), None)
    if llm_feedback is not None and llm_feedback.score_suggestion is not None:
        return llm_feedback.score_suggestion, FeedbackSource.LLM

    auto_feedback = next((item for item in submission.feedback_items if item.source == FeedbackSource.AUTO), None)
    if auto_feedback is not None and auto_feedback.score_suggestion is not None:
        return auto_feedback.score_suggestion, FeedbackSource.AUTO

    result = max(submission.evaluation_results, key=lambda item: item.created_at, default=None)
    if result is None:
        return None, None
    if result.final_score is not None:
        return Decimal(result.final_score), FeedbackSource.AUTO
    if result.auto_score is not None:
        return Decimal(result.auto_score), FeedbackSource.AUTO
    return None, None


def bootstrap_sample_data(db: Session, user: User) -> None:
    existing_membership = db.scalar(select(CourseMember).where(CourseMember.user_id == user.id))
    if existing_membership is not None:
        return

    course = Course(
        code=f"DEMO-{user.id}",
        join_code=generate_join_code(),
        title="Demo Course",
        description="Bootstrap course created automatically for first-time exploration.",
        status=CourseStatus.ACTIVE,
        created_by=user.id,
        updated_at=utcnow(),
    )
    db.add(course)
    db.flush()

    bootstrap_role = CourseRole.TEACHER if user.account_role == AccountRole.TEACHER else CourseRole.STUDENT
    db.add(
        CourseMember(
            course_id=course.id,
            user_id=user.id,
            role=bootstrap_role,
            status=MembershipStatus.ACTIVE,
        )
    )

    assignment = Assignment(
        course_id=course.id,
        title="Week 1 Demo Assignment",
        description="Auto-created assignment to help validate the end-to-end course workflow.",
        status=AssignmentStatus.PUBLISHED,
        published_at=utcnow(),
        allow_late=True,
        default_scoring_rule=ScoringRule.HIGHEST,
        updated_at=utcnow(),
    )
    db.add(assignment)
    db.flush()

    notebook_question = Question(
        assignment_id=assignment.id,
        order_index=1,
        title="Notebook demo question",
        description="Upload a notebook and validate asynchronous evaluation.",
        question_type=QuestionType.NOTEBOOK,
        max_score=Decimal("100"),
        updated_at=utcnow(),
    )
    db.add(notebook_question)
    db.flush()
    db.add(
        NotebookQuestionConfig(
            question_id=notebook_question.id,
            time_limit_seconds=300,
            memory_limit_mb=1024,
            cpu_limit="1",
            allow_network=False,
            visible_tests_source=(
                "result = summary.get('run_success', False)\n"
                "score = 100 if result else 0\n"
                "message = 'Notebook executed successfully.' if result else 'Notebook execution failed.'\n"
            ),
            hidden_tests_source="",
            execution_weight=Decimal('0'),
            visible_weight=Decimal('100'),
            hidden_weight=Decimal('0'),
            updated_at=utcnow(),
        )
    )

    short_answer_question = Question(
        assignment_id=assignment.id,
        order_index=2,
        title="Reflection question",
        description="Describe what your notebook does and any assumptions you made.",
        question_type=QuestionType.SHORT_ANSWER,
        max_score=Decimal("20"),
        updated_at=utcnow(),
    )
    db.add(short_answer_question)
    db.flush()
    db.add(
        ShortAnswerQuestionConfig(
            question_id=short_answer_question.id,
            min_length=20,
            max_length=2000,
            rubric_text="Check clarity, correctness, and completeness.",
            teacher_confirmation_required=True,
            updated_at=utcnow(),
        )
    )
    db.commit()
