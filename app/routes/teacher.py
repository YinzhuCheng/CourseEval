from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Form
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import push_flash
from app.constants import (
    AssignmentStatus,
    CourseRole,
    FeedbackSource,
    MembershipStatus,
    QuestionType,
    ScoringRule,
    SubmissionStatus,
    SubmissionLimitMode,
)
from app.db import get_db, utcnow
from app.models import (
    Assignment,
    Course,
    CourseMember,
    Feedback,
    FinalGradeSnapshot,
    Question,
    ShortAnswerQuestionConfig,
    Submission,
)
from app.services.courses import (
    get_assignment_for_staff,
    get_course_for_staff,
    get_course_for_teacher,
    get_question_for_staff,
    get_question_for_teacher,
)
from app.services.permissions import (
    COURSE_STAFF_ROLES,
    RedirectRequired,
    get_course_role,
    require_login,
    require_teacher_account,
)
from app.services.submissions import (
    get_submission_for_teacher,
    is_submission_pending_teacher_review,
    read_submission_artifact_text,
    refresh_final_grade_snapshot,
)
from app.web import render_template


router = APIRouter(prefix="/teacher", tags=["teacher"])


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(url=location, status_code=303)


@router.get("/courses")
def teacher_courses(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)

    memberships = (
        db.query(CourseMember)
        .join(Course)
        .filter(
            CourseMember.user_id == user.id,
            CourseMember.role.in_(tuple(COURSE_STAFF_ROLES)),
            CourseMember.status == MembershipStatus.ACTIVE,
        )
        .order_by(Course.title.asc())
        .all()
    )
    courses = [membership.course for membership in memberships]
    return render_template(request, db, "teacher_courses.html", {"courses": courses})


@router.post("/courses")
def create_course(
    request: Request,
    code: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)

    code = code.strip().upper()
    title = title.strip()
    if not code or not title:
        push_flash(request, "Course code and title are required.", "danger")
        return _redirect("/teacher/courses")

    if db.query(Course).filter(Course.code == code).first():
        push_flash(request, "Course code already exists.", "danger")
        return _redirect("/teacher/courses")

    course = Course(code=code, title=title, description=description.strip() or None, created_by=user.id)
    db.add(course)
    db.flush()
    db.add(
        CourseMember(
            course_id=course.id,
            user_id=user.id,
            role=CourseRole.TEACHER,
            status=MembershipStatus.ACTIVE,
        )
    )
    db.commit()
    push_flash(request, f"Course {course.code} created.", "success")
    return _redirect(f"/teacher/courses/{course.id}")


@router.get("/courses/{course_id}")
def teacher_course_detail(course_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(request, "You do not have teacher access to this course.", "danger")
        return _redirect("/teacher/courses")

    if course is None:
        push_flash(request, "You do not have teacher access to this course.", "danger")
        return _redirect("/teacher/courses")

    assignments = (
        db.query(Assignment)
        .filter(Assignment.course_id == course.id)
        .order_by(Assignment.created_at.desc())
        .all()
    )
    members = (
        db.query(CourseMember)
        .join(CourseMember.user)
        .filter(CourseMember.course_id == course.id)
        .order_by(CourseMember.joined_at.asc())
        .all()
    )
    course_role = get_course_role(db, course.id, user.id)
    return render_template(
        request,
        db,
        "teacher_course_detail.html",
        {
            "course": course,
            "assignments": assignments,
            "members": members,
            "course_role": course_role,
            "can_manage_course": course_role == CourseRole.TEACHER,
        },
    )


@router.post("/courses/{course_id}/members")
def add_course_member(
    course_id: int,
    request: Request,
    username: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_teacher(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(request, "You do not have teacher access to this course.", "danger")
        return _redirect("/teacher/courses")

    from app.models import User

    member_user = db.query(User).filter(User.username == username.strip()).first()
    if member_user is None:
        push_flash(request, "User not found.", "danger")
        return _redirect(f"/teacher/courses/{course.id}")

    try:
        course_role = CourseRole(role)
    except ValueError:
        push_flash(request, "Invalid course role.", "danger")
        return _redirect(f"/teacher/courses/{course.id}")

    membership = (
        db.query(CourseMember)
        .filter(CourseMember.course_id == course.id, CourseMember.user_id == member_user.id)
        .first()
    )
    if membership is None:
        membership = CourseMember(course_id=course.id, user_id=member_user.id, role=course_role)
        db.add(membership)
    else:
        membership.role = course_role
        membership.status = MembershipStatus.ACTIVE
    db.commit()
    push_flash(request, f"Added {member_user.username} as {course_role.value}.", "success")
    return _redirect(f"/teacher/courses/{course.id}")


@router.post("/courses/{course_id}/assignments")
def create_assignment(
    course_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    status: str = Form(AssignmentStatus.DRAFT.value),
    open_at: str = Form(""),
    due_at: str = Form(""),
    close_at: str = Form(""),
    allow_late: str = Form("false"),
    default_scoring_rule: str = Form(ScoringRule.LATEST.value),
    submission_limit_mode: str = Form(SubmissionLimitMode.UNLIMITED.value),
    submission_limit_value: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_teacher(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(request, "You do not have teacher access to this course.", "danger")
        return _redirect("/teacher/courses")

    try:
        assignment_status = AssignmentStatus(status)
        scoring_rule = ScoringRule(default_scoring_rule)
        limit_mode = SubmissionLimitMode(submission_limit_mode)
    except ValueError:
        push_flash(request, "Invalid assignment settings.", "danger")
        return _redirect(f"/teacher/courses/{course.id}")

    assignment = Assignment(
        course_id=course.id,
        title=title.strip(),
        description=description.strip() or None,
        status=assignment_status,
        open_at=_parse_datetime_input(open_at),
        due_at=_parse_datetime_input(due_at),
        close_at=_parse_datetime_input(close_at),
        allow_late=allow_late == "true",
        default_scoring_rule=scoring_rule,
        submission_limit_mode=limit_mode,
        submission_limit_value=int(submission_limit_value) if submission_limit_value.strip() else None,
        published_at=utcnow() if assignment_status == AssignmentStatus.PUBLISHED else None,
    )
    db.add(assignment)
    db.commit()
    push_flash(request, f"Assignment {assignment.title} created.", "success")
    return _redirect(f"/teacher/assignments/{assignment.id}")


@router.get("/assignments/{assignment_id}")
def teacher_assignment_detail(assignment_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        assignment = get_assignment_for_staff(db, assignment_id, user.id)
        if assignment is None:
            raise PermissionError
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(request, "You do not have teacher access to this assignment.", "danger")
        return _redirect("/teacher/courses")

    questions = (
        db.query(Question)
        .filter(Question.assignment_id == assignment.id)
        .order_by(Question.order_index.asc(), Question.id.asc())
        .all()
    )
    submissions = (
        db.query(Submission)
        .filter(Submission.assignment_id == assignment.id)
        .order_by(Submission.submitted_at.desc())
        .limit(20)
        .all()
    )
    course_role = get_course_role(db, assignment.course_id, user.id)
    return render_template(
        request,
        db,
        "teacher_assignment_detail.html",
        {
            "assignment": assignment,
            "questions": questions,
            "submissions": submissions,
            "course_role": course_role,
            "can_manage_course": course_role == CourseRole.TEACHER,
        },
    )


@router.post("/assignments/{assignment_id}/questions")
def create_question(
    assignment_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    question_type: str = Form(...),
    max_score: str = Form("100"),
    scoring_rule_override: str = Form(""),
    time_limit_seconds: str = Form("300"),
    memory_limit_mb: str = Form("1024"),
    cpu_limit: str = Form("1"),
    allow_network: str = Form("false"),
    visible_tests_source: str = Form(""),
    hidden_tests_source: str = Form(""),
    execution_weight: str = Form("0"),
    visible_weight: str = Form("100"),
    hidden_weight: str = Form("0"),
    llm_score_weight: str = Form("0"),
    llm_scoring_rubric: str = Form(""),
    llm_feedback_enabled: str = Form("false"),
    rubric_text: str = Form(""),
    min_length: str = Form(""),
    max_length: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        assignment = (
            db.query(Assignment)
            .join(Course)
            .join(CourseMember, CourseMember.course_id == Course.id)
            .filter(
                Assignment.id == assignment_id,
                CourseMember.user_id == user.id,
                CourseMember.role == CourseRole.TEACHER,
                CourseMember.status == MembershipStatus.ACTIVE,
            )
            .first()
        )
        if assignment is None:
            raise PermissionError
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(request, "You do not have teacher access to this assignment.", "danger")
        return _redirect("/teacher/courses")

    try:
        q_type = QuestionType(question_type)
    except ValueError:
        push_flash(request, "Invalid question type.", "danger")
        return _redirect(f"/teacher/assignments/{assignment.id}")

    max_score_decimal = Decimal(max_score)
    order_index = len(assignment.questions) + 1
    question = Question(
        assignment_id=assignment.id,
        order_index=order_index,
        title=title.strip(),
        description=description.strip() or None,
        question_type=q_type,
        max_score=max_score_decimal,
        scoring_rule_override=ScoringRule(scoring_rule_override) if scoring_rule_override else None,
    )
    db.add(question)
    db.flush()

    if q_type == QuestionType.NOTEBOOK:
        from app.models import NotebookQuestionConfig

        db.add(
            NotebookQuestionConfig(
                question_id=question.id,
                time_limit_seconds=int(time_limit_seconds or 300),
                memory_limit_mb=int(memory_limit_mb or 1024),
                cpu_limit=cpu_limit or "1",
                allow_network=allow_network == "true",
                visible_tests_source=visible_tests_source.strip() or None,
                hidden_tests_source=hidden_tests_source.strip() or None,
                execution_weight=Decimal(execution_weight or "0"),
                visible_weight=Decimal(visible_weight or "100"),
                hidden_weight=Decimal(hidden_weight or "0"),
                llm_score_weight=Decimal(llm_score_weight or "0"),
                llm_scoring_rubric=llm_scoring_rubric.strip() or None,
                llm_feedback_enabled=llm_feedback_enabled == "true",
            )
        )
    else:
        db.add(
            ShortAnswerQuestionConfig(
                question_id=question.id,
                min_length=int(min_length) if min_length.strip() else None,
                max_length=int(max_length) if max_length.strip() else None,
                rubric_text=rubric_text.strip() or None,
            )
        )

    db.commit()
    push_flash(request, f"Question {question.title} created.", "success")
    return _redirect(f"/teacher/questions/{question.id}")


@router.get("/questions/{question_id}")
def teacher_question_detail(question_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        question = get_question_for_staff(db, question_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(request, "You do not have teacher access to this question.", "danger")
        return _redirect("/teacher/courses")

    if question is None:
        push_flash(request, "You do not have teacher access to this question.", "danger")
        return _redirect("/teacher/courses")

    submissions = (
        db.query(Submission)
        .filter(Submission.question_id == question.id)
        .order_by(Submission.submitted_at.desc())
        .all()
    )
    snapshots = (
        db.query(FinalGradeSnapshot)
        .filter(FinalGradeSnapshot.question_id == question.id)
        .order_by(FinalGradeSnapshot.updated_at.desc())
        .all()
    )
    course_role = get_course_role(db, question.assignment.course_id, user.id)
    return render_template(
        request,
        db,
        "teacher_question_detail.html",
        {
            "question": question,
            "submissions": submissions,
            "snapshots": snapshots,
            "course_role": course_role,
            "can_manage_course": course_role == CourseRole.TEACHER,
        },
    )


@router.get("/submissions/{submission_id}")
def teacher_submission_detail(submission_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        submission = get_submission_for_teacher(db, submission_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(request, "You do not have teacher access to this submission.", "danger")
        return _redirect("/teacher/courses")

    latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
    feedback_items = sorted(submission.feedback_items, key=lambda item: item.created_at, reverse=True)
    course_role = get_course_role(db, submission.course_id, user.id)
    return render_template(
        request,
        db,
        "teacher_submission_detail.html",
        {
            "submission": submission,
            "latest_result": latest_result,
            "feedback_items": feedback_items,
            "pending_teacher_review": is_submission_pending_teacher_review(submission),
            "can_grade_submission": course_role == CourseRole.TEACHER,
            "stdout_text": read_submission_artifact_text(latest_result, "stdout") if latest_result else "",
            "stderr_text": read_submission_artifact_text(latest_result, "stderr") if latest_result else "",
        },
    )


@router.post("/submissions/{submission_id}/grade")
def grade_submission(
    submission_id: int,
    request: Request,
    score: str = Form(""),
    comment_text: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        submission = get_submission_for_teacher(db, submission_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(request, "You do not have teacher access to this submission.", "danger")
        return _redirect("/teacher/courses")

    if get_course_role(db, submission.course_id, user.id) != CourseRole.TEACHER:
        push_flash(request, "Only teachers can save final grading decisions for this course.", "danger")
        return _redirect(f"/teacher/submissions/{submission.id}")

    score_value = Decimal(score) if score.strip() else None
    requires_teacher_score = submission.submission_type == QuestionType.SHORT_ANSWER
    if requires_teacher_score and score_value is None:
        push_flash(request, "A score is required when saving teacher feedback.", "danger")
        return _redirect(f"/teacher/submissions/{submission.id}")
    if score_value is not None and (score_value < 0 or score_value > Decimal(str(submission.question.max_score))):
        push_flash(request, f"Score must be between 0 and {submission.question.max_score}.", "danger")
        return _redirect(f"/teacher/submissions/{submission.id}")

    latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
    feedback = Feedback(
        submission_id=submission.id,
        evaluation_result_id=latest_result.id if latest_result else None,
        source=FeedbackSource.TEACHER,
        score_suggestion=score_value,
        comment_text=comment_text.strip() or None,
        created_by=user.id,
    )
    db.add(feedback)
    if is_submission_pending_teacher_review(submission):
        submission.status = SubmissionStatus.COMPLETED
        submission.completed_at = utcnow()
        submission.is_effective_submission = True
        submission.failure_reason_code = None
    db.commit()
    refresh_final_grade_snapshot(db, submission.question_id, submission.user_id)
    push_flash(request, "Teacher feedback saved.", "success")
    return _redirect(f"/teacher/submissions/{submission.id}")


def _parse_datetime_input(value: str):
    value = value.strip()
    if not value:
        return None
    normalized = value.replace("T", " ")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
