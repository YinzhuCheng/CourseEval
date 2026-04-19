import json
import secrets
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, joinedload

from app.auth import is_admin
from app.constants import (
    AccountRole,
    AssignmentStatus,
    CourseRole,
    CourseStatus,
    MembershipStatus,
    PlatformRole,
    QuestionType,
    ScoringRule,
)
from app.db import utcnow
from app.models import (
    Assignment,
    Course,
    CourseMember,
    FileQuestionConfig,
    FinalGradeSnapshot,
    LLMConfig,
    CodeQuestionConfig,
    Question,
    QuestionVersion,
    RuntimeImage,
    ShortAnswerQuestionConfig,
    Submission,
    User,
)
from app.runtime_support import default_allowed_code_libraries_text


STAFF_COURSE_ROLES = (CourseRole.TEACHER, CourseRole.TA)
OPEN_COMMUNITY_COURSE_CODE = "__OPEN_COMMUNITY__"
DEFAULT_ALLOWED_CODE_LIBRARIES = default_allowed_code_libraries_text("en")
DEFAULT_ALLOWED_PYTHON_LIBRARIES = DEFAULT_ALLOWED_CODE_LIBRARIES
# Keep the legacy name as an alias so older imports and payload builders stay valid.
DEFAULT_ALLOWED_LIBRARIES = DEFAULT_ALLOWED_PYTHON_LIBRARIES


def _question_loader_options():
    return (
        joinedload(Question.code_config),
        joinedload(Question.short_answer_config),
        joinedload(Question.file_question_config),
        joinedload(Question.versions),
    )


def _assignment_question_loader_options():
    return (
        joinedload(Assignment.questions).joinedload(Question.code_config),
        joinedload(Assignment.questions).joinedload(Question.short_answer_config),
        joinedload(Assignment.questions).joinedload(Question.file_question_config),
    )


def generate_join_code() -> str:
    return secrets.token_hex(3).upper()


def is_open_community_course(course: Course | None) -> bool:
    return bool(course and getattr(course, "is_open_community", False))


def sort_courses_for_display(courses: list[Course]) -> list[Course]:
    """Pin the platform open community course first, then alphabetical by title."""
    return sorted(
        courses,
        key=lambda c: (0 if is_open_community_course(c) else 1, (c.title or "").lower(), c.id),
    )


def ensure_user_in_open_community_course(db: Session, user: User) -> None:
    """Add active verified users to the platform open community course (student role)."""
    if not user.is_active or not user.email_verified:
        return
    course = db.scalar(select(Course).where(Course.code == OPEN_COMMUNITY_COURSE_CODE))
    if course is None or not course.is_open_community:
        return
    row = db.scalar(select(CourseMember).where(CourseMember.course_id == course.id, CourseMember.user_id == user.id))
    if row is None:
        db.add(
            CourseMember(
                course_id=course.id,
                user_id=user.id,
                role=CourseRole.STUDENT,
                status=MembershipStatus.ACTIVE,
            )
        )
    else:
        row.status = MembershipStatus.ACTIVE
        row.role = CourseRole.STUDENT


def list_courses_for_student(db: Session, user_id: int) -> list[Course]:
    statement = (
        select(Course)
        .join(CourseMember, CourseMember.course_id == Course.id)
        .where(
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
        .order_by(
            case((Course.is_open_community.is_(True), 0), else_=1).asc(),
            Course.title.asc(),
        )
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
            *_assignment_question_loader_options(),
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
            *_question_loader_options(),
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
            joinedload(Course.default_llm_config),
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
            *_question_loader_options(),
        )
        .join(Assignment, Question.assignment_id == Assignment.id)
        .join(Course, Course.id == Assignment.course_id)
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
    if is_admin(user):
        statement = select(Course).order_by(
            case((Course.is_open_community.is_(True), 0), else_=1).asc(),
            Course.title.asc(),
        )
    else:
        statement = (
            select(Course)
            .join(CourseMember, CourseMember.course_id == Course.id)
            .where(CourseMember.user_id == user.id, CourseMember.status == MembershipStatus.ACTIVE)
            .order_by(
                case((Course.is_open_community.is_(True), 0), else_=1).asc(),
                Course.title.asc(),
            )
        )
    return list(db.scalars(statement).unique())


def get_course_for_staff(db: Session, course_id: int, user_id: int) -> Course | None:
    statement = (
        select(Course)
        .options(
            joinedload(Course.assignments).joinedload(Assignment.questions),
            joinedload(Course.members).joinedload(CourseMember.user),
            joinedload(Course.default_llm_config),
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
            *_assignment_question_loader_options(),
        )
        .join(CourseMember, CourseMember.course_id == Assignment.course_id)
        .join(Course, Course.id == Assignment.course_id)
        .where(
            Assignment.id == assignment_id,
            CourseMember.user_id == user_id,
            CourseMember.role.in_(STAFF_COURSE_ROLES),
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def get_question_for_staff(db: Session, question_id: int, user_id: int) -> Question | None:
    statement = (
        select(Question)
        .options(
            joinedload(Question.assignment).joinedload(Assignment.course),
            *_question_loader_options(),
        )
        .join(Assignment, Assignment.id == Question.assignment_id)
        .join(Course, Course.id == Assignment.course_id)
        .join(CourseMember, CourseMember.course_id == Assignment.course_id)
        .where(
            Question.id == question_id,
            CourseMember.user_id == user_id,
            CourseMember.role.in_(STAFF_COURSE_ROLES),
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
    normalized_code = code.strip().upper()
    if normalized_code == OPEN_COMMUNITY_COURSE_CODE:
        raise ValueError("Reserved course code.")
    course = Course(
        code=normalized_code,
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
    if course.is_open_community:
        raise ValueError("This course does not use a join code.")

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
            *_assignment_question_loader_options(),
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
    code_config_payload: dict | None,
    short_answer_payload: dict | None,
    file_question_payload: dict | None,
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

    if question_type == QuestionType.CODE:
        payload = code_config_payload or {}
        db.add(
            CodeQuestionConfig(
                question_id=question.id,
                input_spec=payload.get("input_spec") or None,
                output_spec=payload.get("output_spec") or None,
                visible_tests_json=payload.get("visible_tests_json", "[]"),
                hidden_tests_json=payload.get("hidden_tests_json", "[]"),
                allowed_libraries_note=payload.get("allowed_libraries_note")
                or DEFAULT_ALLOWED_CODE_LIBRARIES,
                allowed_languages_json=payload.get("allowed_languages_json", '["python"]'),
                reference_solution_python=payload.get("reference_solution_python", ""),
                reference_solution_c=payload.get("reference_solution_c", ""),
                reference_solution_cpp=payload.get("reference_solution_cpp", ""),
                time_limit_seconds=payload.get("time_limit_seconds", 10),
                memory_limit_mb=payload.get("memory_limit_mb", 512),
                cpu_limit=payload.get("cpu_limit", "1"),
                allow_network=payload.get("allow_network", False),
                updated_at=utcnow(),
            )
        )
    elif question_type == QuestionType.SHORT_ANSWER:
        payload = short_answer_payload or {}
        db.add(
            ShortAnswerQuestionConfig(
                question_id=question.id,
                min_length=payload.get("min_length"),
                max_length=payload.get("max_length"),
                rubric_text=payload.get("rubric_text") or None,
                llm_suggestion_enabled=payload.get("llm_suggestion_enabled", False),
                teacher_confirmation_required=payload.get("teacher_confirmation_required", False),
                updated_at=utcnow(),
            )
        )
    elif question_type == QuestionType.FILE_LLM:
        payload = file_question_payload or {}
        db.add(
            FileQuestionConfig(
                question_id=question.id,
                accepted_extensions=payload.get("accepted_extensions", ".pdf"),
                reference_answer_text=payload.get("reference_answer_text", ""),
                rubric_text=payload.get("rubric_text", ""),
                llm_suggestion_enabled=payload.get("llm_suggestion_enabled", True),
                teacher_confirmation_required=payload.get("teacher_confirmation_required", False),
                notebook_outputs_required=payload.get("notebook_outputs_required", True),
                updated_at=utcnow(),
            )
        )

    db.commit()
    statement = (
        select(Question)
        .options(*_question_loader_options())
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
        max_llm_retries=3,
        llm_retry_initial_seconds=5,
        enabled=enabled,
        created_by=creator_id,
        updated_at=utcnow(),
    )
    db.add(llm_config)
    db.commit()
    db.refresh(llm_config)
    return llm_config


def summarize_course_grade_matrix(db: Session, course_id: int) -> dict:
    """Students × assignments: submission flag and summed snapshot scores."""
    student_rows = (
        db.execute(
            select(User.id, User.username)
            .join(CourseMember, CourseMember.user_id == User.id)
            .where(
                CourseMember.course_id == course_id,
                CourseMember.role == CourseRole.STUDENT,
                CourseMember.status == MembershipStatus.ACTIVE,
            )
            .order_by(User.username.asc())
        )
        .all()
    )
    assignment_rows = (
        db.execute(
            select(Assignment.id, Assignment.title)
            .where(Assignment.course_id == course_id)
            .order_by(Assignment.created_at.desc())
        )
        .all()
    )
    students = [{"id": row[0], "username": row[1]} for row in student_rows]
    assignments = [{"id": row[0], "title": row[1]} for row in assignment_rows]
    if not students or not assignments:
        return {"assignments": assignments, "rows": []}

    assignment_ids = [a["id"] for a in assignments]
    student_ids = [s["id"] for s in students]

    totals_stmt = (
        select(
            FinalGradeSnapshot.student_id,
            FinalGradeSnapshot.assignment_id,
            func.coalesce(func.sum(FinalGradeSnapshot.score), 0),
        )
        .where(
            FinalGradeSnapshot.assignment_id.in_(assignment_ids),
            FinalGradeSnapshot.student_id.in_(student_ids),
        )
        .group_by(FinalGradeSnapshot.student_id, FinalGradeSnapshot.assignment_id)
    )
    totals_map: dict[tuple[int, int], Decimal] = {}
    for row in db.execute(totals_stmt).all():
        totals_map[(int(row[0]), int(row[1]))] = row[2] if isinstance(row[2], Decimal) else Decimal(str(row[2]))

    sub_stmt = (
        select(Submission.user_id, Submission.assignment_id, func.count(Submission.id))
        .where(
            Submission.assignment_id.in_(assignment_ids),
            Submission.user_id.in_(student_ids),
        )
        .group_by(Submission.user_id, Submission.assignment_id)
    )
    submitted_map: dict[tuple[int, int], int] = {}
    for row in db.execute(sub_stmt).all():
        submitted_map[(int(row[0]), int(row[1]))] = int(row[2] or 0)

    matrix_rows = []
    for st in students:
        row_cells = []
        for asn in assignments:
            key = (st["id"], asn["id"])
            row_cells.append(
                {
                    "submitted": submitted_map.get(key, 0) > 0,
                    "total_score": totals_map.get(key, Decimal("0")),
                }
            )
        matrix_rows.append({"student": st, "cells": row_cells})
    return {"assignments": assignments, "rows": matrix_rows}


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


def bootstrap_sample_data(db: Session, user: User) -> None:
    existing_membership = db.scalar(select(CourseMember).where(CourseMember.user_id == user.id))
    if existing_membership is not None:
        return

    course = Course(
        code=f"DS-{user.id}",
        join_code=generate_join_code(),
        title="数据结构",
        description="默认调试课程，覆盖 Python 代码题、PDF 题和格式化文本题三种提交模式。",
        status=CourseStatus.ACTIVE,
        created_by=user.id,
        updated_at=utcnow(),
    )
    db.add(course)
    db.flush()

    bootstrap_role = (
        CourseRole.TEACHER
        if user.account_role == AccountRole.TEACHER or user.platform_role in {PlatformRole.ADMIN, PlatformRole.SUPER_ADMIN}
        else CourseRole.STUDENT
    )
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
        title="第一次作业",
        description="用于调试三种新提交模式的默认作业。",
        status=AssignmentStatus.PUBLISHED,
        published_at=utcnow(),
        allow_late=True,
        default_scoring_rule=ScoringRule.HIGHEST,
        updated_at=utcnow(),
    )
    db.add(assignment)
    db.flush()

    code_question = Question(
        assignment_id=assignment.id,
        order_index=1,
        title="括号匹配判断",
        description=(
            "输入一行只包含 ()[]{} 的字符串，输出 YES 或 NO，判断括号是否完全匹配。"
            "\n请严格按照输入输出格式编写程序，可使用 Python、C 或 C++。"
        ),
        question_type=QuestionType.CODE,
        max_score=Decimal("100"),
        updated_at=utcnow(),
    )
    db.add(code_question)
    db.flush()
    db.add(
        CodeQuestionConfig(
            question_id=code_question.id,
            input_spec="输入一行括号字符串，例如 ()[]{}",
            output_spec="若括号完全匹配输出 YES，否则输出 NO",
            visible_tests_json=json.dumps(
                [
                    {"input": "()[]{}", "expected_output": "YES", "points": 20},
                    {"input": "([{}])", "expected_output": "YES", "points": 20},
                    {"input": "([)]", "expected_output": "NO", "points": 20},
                ],
                ensure_ascii=False,
                indent=2,
            ),
            hidden_tests_json=json.dumps(
                [
                    {"input": "(((", "expected_output": "NO", "points": 20},
                    {"input": "{[()()]}", "expected_output": "YES", "points": 20},
                ],
                ensure_ascii=False,
                indent=2,
            ),
            allowed_libraries_note=DEFAULT_ALLOWED_CODE_LIBRARIES,
            allowed_languages_json='["python", "c", "cpp"]',
            reference_solution_python=(
                "s = input().strip()\n"
                "stack = []\n"
                "pairs = {')': '(', ']': '[', '}': '{'}\n"
                "ok = True\n"
                "for ch in s:\n"
                "    if ch in '([{':\n"
                "        stack.append(ch)\n"
                "    elif not stack or stack.pop() != pairs[ch]:\n"
                "        ok = False\n"
                "        break\n"
                "print('YES' if ok and not stack else 'NO')\n"
            ),
            reference_solution_c=(
                "#include <stdio.h>\n"
                "#include <string.h>\n\n"
                "int main(void) {\n"
                "    char s[10005], st[10005];\n"
                "    if (scanf(\"%10004s\", s) != 1) return 0;\n"
                "    int top = 0, ok = 1;\n"
                "    for (int i = 0; s[i]; ++i) {\n"
                "        char c = s[i];\n"
                "        if (c == '(' || c == '[' || c == '{') st[top++] = c;\n"
                "        else {\n"
                "            if (top == 0) { ok = 0; break; }\n"
                "            char p = st[--top];\n"
                "            if ((c == ')' && p != '(') || (c == ']' && p != '[') || (c == '}' && p != '{')) { ok = 0; break; }\n"
                "        }\n"
                "    }\n"
                "    printf(\"%s\\n\", ok && top == 0 ? \"YES\" : \"NO\");\n"
                "    return 0;\n"
                "}\n"
            ),
            reference_solution_cpp=(
                "#include <iostream>\n"
                "#include <map>\n"
                "#include <string>\n"
                "#include <vector>\n"
                "using namespace std;\n\n"
                "int main() {\n"
                "    string s;\n"
                "    if (!(cin >> s)) return 0;\n"
                "    vector<char> st;\n"
                "    map<char, char> pairs{{')','('}, {']','['}, {'}','{'}};\n"
                "    bool ok = true;\n"
                "    for (char c : s) {\n"
                "        if (c == '(' || c == '[' || c == '{') st.push_back(c);\n"
                "        else {\n"
                "            if (st.empty() || st.back() != pairs[c]) { ok = false; break; }\n"
                "            st.pop_back();\n"
                "        }\n"
                "    }\n"
                "    cout << (ok && st.empty() ? \"YES\" : \"NO\") << '\\n';\n"
                "    return 0;\n"
                "}\n"
            ),
            time_limit_seconds=300,
            memory_limit_mb=1024,
            cpu_limit="1",
            allow_network=False,
            updated_at=utcnow(),
        )
    )

    pdf_question = Question(
        assignment_id=assignment.id,
        order_index=2,
        title="栈与队列概念比较（PDF）",
        description="请提交 PDF，比较栈和队列的定义、典型操作以及一个实际应用场景。",
        question_type=QuestionType.FILE_LLM,
        max_score=Decimal("20"),
        updated_at=utcnow(),
    )
    db.add(pdf_question)
    db.flush()
    db.add(
        FileQuestionConfig(
            question_id=pdf_question.id,
            accepted_extensions=".pdf",
            rubric_text="比较定义是否准确、操作描述是否完整、应用场景是否合理，按 20 分评分。",
                reference_answer_text=(
                "栈是后进先出（LIFO），队列是先进先出（FIFO）；"
                "栈常见操作有 push/pop/top，队列常见操作有 enqueue/dequeue/front；"
                "应用场景可举函数调用栈、任务排队等。"
            ),
            llm_suggestion_enabled=True,
            teacher_confirmation_required=False,
            notebook_outputs_required=False,
            updated_at=utcnow(),
        )
    )

    formatted_question = Question(
        assignment_id=assignment.id,
        order_index=3,
        title="顺序表与链表复杂度分析（文本/TeX/ipynb）",
        description="请提交 txt、tex 或已执行输出的 ipynb，说明顺序表和链表在随机访问、插入、删除上的复杂度差异。",
        question_type=QuestionType.FILE_LLM,
        max_score=Decimal("20"),
        updated_at=utcnow(),
    )
    db.add(formatted_question)
    db.flush()
    db.add(
        FileQuestionConfig(
            question_id=formatted_question.id,
            accepted_extensions=".txt,.tex,.ipynb",
            rubric_text="关注复杂度结论、原因解释和表达清晰度，按 20 分评分。",
                reference_answer_text=(
                "顺序表支持 O(1) 随机访问，但中间插入删除通常为 O(n)；"
                "链表随机访问为 O(n)，但已定位节点后插入删除可达 O(1)。"
            ),
            llm_suggestion_enabled=True,
            teacher_confirmation_required=False,
            notebook_outputs_required=True,
            updated_at=utcnow(),
        )
    )
    db.commit()
