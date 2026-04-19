import json
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import push_flash
from app.constants import (
    AssignmentStatus,
    CodeLanguage,
    CourseRole,
    FeedbackSource,
    LLMResponseLanguage,
    MembershipStatus,
    QuestionType,
    ScoringRule,
    SubmissionLimitMode,
    SubmissionStatus,
)
from app.db import get_db, utcnow
from app.i18n import choose_text, get_locale
from app.config import get_settings
from app.models import (
    Assignment,
    Course,
    CourseMember,
    CourseDiscussionMute,
    DiscussionPost,
    DiscussionTopic,
    Feedback,
    FinalGradeSnapshot,
    FileQuestionConfig,
    LLMConfig,
    CodeQuestionConfig,
    Question,
    QuestionVersion,
    ShortAnswerQuestionConfig,
    Submission,
    User,
)
from app.runtime_support import default_allowed_code_libraries_text
from app.services.course_materials import list_materials_for_course
from app.services.courses import (
    DEFAULT_ALLOWED_CODE_LIBRARIES,
    OPEN_COMMUNITY_COURSE_CODE,
    ensure_user_in_open_community_course,
    get_assignment_for_staff,
    get_course_for_staff,
    get_course_for_teacher,
    get_question_for_staff,
    get_question_for_teacher,
    summarize_course_grade_matrix,
)
from app.services.permissions import (
    COURSE_STAFF_ROLES,
    RedirectRequired,
    get_course_role,
    require_login,
    require_teacher_account,
)
from app.services.discussion_ai import create_user_post_and_maybe_ai_reply
from app.services.discussion_attachments import attach_discussion_images_to_post, delete_discussion_attachment_files
from app.services.discussion_forms import extract_discussion_images
from app.services.image_uploads import (
    ALLOWED_IMAGE_EXTENSIONS,
    format_image_upload_error,
    human_upload_max_bytes,
)
from app.services.discussions import (
    build_discussion_view_context,
    can_moderate_discussion,
    can_post_on_question_topic,
    get_or_create_question_topic,
    hard_delete_post,
    mute_user_in_course,
    unmute_user_in_course,
)
from app.services.post_close_reveal import reveal_bundle_for_question
from app.services.question_versions import append_question_version_after_edit, create_initial_question_version
from app.services.redirects import safe_local_redirect
from app.services.scoring import is_submission_pending_teacher_review
from app.services.llm_groups import group_has_callable_target
from app.services.submissions import (
    get_submission_for_teacher,
    read_submission_artifact_text,
    refresh_final_grade_snapshot,
    store_reference_answer_file,
)
from app.services.user_storage import purge_submission_as_viewer
from app.services.teacher_analytics import (
    active_student_ids,
    compute_assignment_staff_stats,
    compute_course_staff_overview,
    compute_question_class_stats,
    enrich_course_grade_matrix,
    grade_summary_from_float_scores,
    percentile_rank,
    score_distribution_by_question,
)
from app.web import render_template


router = APIRouter(prefix="/teacher", tags=["teacher"])
settings = get_settings()
display_timezone = ZoneInfo(settings.timezone_name)


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(url=location, status_code=303)


def _parse_decimal_input(raw: str, field_label: str) -> Decimal:
    try:
        return Decimal((raw or "").strip())
    except Exception as exc:
        raise ValueError(f"{field_label} must be a valid number.") from exc


def _parse_int_input(
    raw: str,
    field_label: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    text = (raw or "").strip()
    if not text and default is not None:
        value = default
    else:
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(f"{field_label} must be a whole number.") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_label} is below the allowed minimum.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_label} is above the allowed maximum.")
    return value


def _normalize_cpu_limit(raw: str, *, default: str = "1") -> str:
    text = (raw or "").strip() or default
    try:
        value = Decimal(text)
    except Exception as exc:
        raise ValueError("CPU limit must be a valid number.") from exc
    if value <= 0 or value > Decimal("4"):
        raise ValueError("CPU limit must be greater than 0 and no more than 4.")
    return text


def _parse_code_runner_limits(time_limit_seconds: str, memory_limit_mb: str, cpu_limit: str) -> tuple[int, int, str]:
    return (
        _parse_int_input(time_limit_seconds, "Time limit", default=300, minimum=1, maximum=600),
        _parse_int_input(memory_limit_mb, "Memory limit", default=1024, minimum=128, maximum=4096),
        _normalize_cpu_limit(cpu_limit),
    )


def _parse_optional_length(raw: str, field_label: str) -> int | None:
    if not (raw or "").strip():
        return None
    return _parse_int_input(raw, field_label, minimum=0, maximum=200000)


@router.get("/courses")
def teacher_courses(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)

    ensure_user_in_open_community_course(db, user)
    db.commit()
    memberships = (
        db.query(CourseMember)
        .join(Course)
        .filter(
            CourseMember.user_id == user.id,
            CourseMember.role.in_(tuple(COURSE_STAFF_ROLES)),
            CourseMember.status == MembershipStatus.ACTIVE,
            Course.is_hidden_from_course_lists.is_(False),
        )
        .order_by(
            case((Course.is_open_community.is_(True), 0), else_=1).asc(),
            Course.title.asc(),
        )
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
        push_flash(
            request,
            choose_text(request, "Course code and title are required.", "课程编号和课程标题不能为空。"),
            "danger",
        )
        return _redirect("/teacher/courses")

    if db.query(Course).filter(Course.code == code).first():
        push_flash(request, choose_text(request, "Course code already exists.", "课程编号已存在。"), "danger")
        return _redirect("/teacher/courses")

    if code == OPEN_COMMUNITY_COURSE_CODE:
        push_flash(request, choose_text(request, "Reserved course code.", "该课程编号为系统保留。"), "danger")
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
    push_flash(
        request,
        choose_text(request, f"Course {course.code} was created.", f"课程 {course.code} 已创建。"),
        "success",
    )
    return _redirect(f"/teacher/courses/{course.id}")


@router.post("/courses/{course_id}/cover")
async def upload_course_cover(
    course_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_teacher(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/teacher/courses")
    raw = await file.read()
    from app.services.user_media import store_course_cover_image

    try:
        course.cover_image_path = store_course_cover_image(course.id, raw, file.filename or "cover.png")
    except ValueError as exc:
        key = str(exc) if exc else ""
        if key in ("unsupported_image_type", "file_too_large"):
            push_flash(request, format_image_upload_error(request, key), "danger")
        else:
            push_flash(request, choose_text(request, "Invalid image file.", "图片无效。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}")
    course.updated_at = utcnow()
    db.commit()
    push_flash(request, choose_text(request, "Course image updated.", "课程图片已更新。"), "success")
    return _redirect(f"/teacher/courses/{course_id}")


@router.post("/courses/{course_id}/cover/remove")
def remove_course_cover(course_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_teacher(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None:
        return _redirect("/teacher/courses")
    from app.services.user_media import clear_course_cover_files

    clear_course_cover_files(course.id)
    course.cover_image_path = None
    course.updated_at = utcnow()
    db.commit()
    push_flash(request, choose_text(request, "Course image removed.", "已移除课程图片。"), "success")
    return _redirect(f"/teacher/courses/{course_id}")


@router.get("/courses/{course_id}")
def teacher_course_detail(course_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(
            request,
            choose_text(request, "You do not have teacher access to this course.", "你没有该课程的教师端访问权限。"),
            "danger",
        )
        return _redirect("/teacher/courses")

    if course is None:
        push_flash(
            request,
            choose_text(request, "You do not have teacher access to this course.", "你没有该课程的教师端访问权限。"),
            "danger",
        )
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
    available_llm_configs = list(
        db.query(LLMConfig)
        .options(selectinload(LLMConfig.members))
        .filter(
            LLMConfig.enabled.is_(True),
            LLMConfig.scope == "platform",
        )
        .order_by(LLMConfig.last_tested_at.desc(), LLMConfig.created_at.desc())
        .all()
    )
    available_llm_configs = [group for group in available_llm_configs if group_has_callable_target(group)]
    course_role = get_course_role(db, course.id, user.id)
    grade_matrix = None
    course_staff_overview = None
    materials = list_materials_for_course(db, course.id)
    if course_role in (CourseRole.TEACHER, CourseRole.TA):
        course_staff_overview = compute_course_staff_overview(db, course.id)
    if course_role == CourseRole.TEACHER:
        grade_matrix = enrich_course_grade_matrix(db, course.id, summarize_course_grade_matrix(db, course.id))
    staff_can_mod = can_moderate_discussion(db, course.id, user)
    mute_rows = list(db.scalars(select(CourseDiscussionMute).where(CourseDiscussionMute.course_id == course.id)).all())
    discussion_mute_by_user = {m.user_id: m for m in mute_rows}
    cover_exts = ", ".join(sorted(e.replace(".", "").upper() for e in sorted(ALLOWED_IMAGE_EXTENSIONS)))
    cover_max = human_upload_max_bytes()
    return render_template(
        request,
        db,
        "teacher_course_detail.html",
        {
            "course": course,
            "assignments": assignments,
            "materials": materials,
            "members": members,
            "course_role": course_role,
            "can_manage_course": course_role == CourseRole.TEACHER,
            "available_llm_configs": available_llm_configs,
            "grade_matrix": grade_matrix,
            "course_staff_overview": course_staff_overview,
            "can_moderate_discussion": staff_can_mod,
            "discussion_moderation_course_id": course.id,
            "discussion_mute_by_user": discussion_mute_by_user,
            "course_cover_image_rules_en": (
                f"Formats: {cover_exts} (extension must match image contents). "
                f"Max {cover_max} per file after processing."
            ),
            "course_cover_image_rules_zh": (
                f"格式：{cover_exts}（扩展名需与实际图像一致）。处理后单文件不超过 {cover_max}。"
            ),
        },
    )


@router.post("/courses/{course_id}/llm-config")
def update_course_llm_config(
    course_id: int,
    request: Request,
    llm_config_id: str = Form(""),
    llm_response_language: str = Form(LLMResponseLanguage.AUTO.value),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_teacher(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(
            request,
            choose_text(request, "You do not have teacher access to this course.", "你没有该课程的教师端访问权限。"),
            "danger",
        )
        return _redirect("/teacher/courses")

    selected_id = llm_config_id.strip()
    try:
        course.llm_response_language = LLMResponseLanguage(llm_response_language.strip().lower()).value
    except ValueError:
        course.llm_response_language = LLMResponseLanguage.AUTO.value

    if not selected_id:
        course.default_llm_config_id = None
        course.use_global_llm_default = True
        db.commit()
        push_flash(
            request,
            choose_text(
                request,
                "This course now follows the latest platform-tested LLM by default.",
                "该课程已改为默认跟随平台最新测试成功的 LLM 配置。",
            ),
            "success",
        )
        return _redirect(f"/teacher/courses/{course.id}")

    try:
        config_id = int(selected_id)
    except ValueError:
        push_flash(
            request,
            choose_text(request, "Selected LLM config is invalid.", "所选 LLM 配置无效。"),
            "danger",
        )
        return _redirect(f"/teacher/courses/{course.id}")

    config = db.get(LLMConfig, config_id)
    if config is None or not group_has_callable_target(config):
        push_flash(
            request,
            choose_text(request, "Selected LLM config is unavailable.", "所选 LLM 配置不可用。"),
            "danger",
        )
        return _redirect(f"/teacher/courses/{course.id}")

    course.default_llm_config_id = config.id
    course.use_global_llm_default = False
    db.commit()
    push_flash(
        request,
        choose_text(
            request,
            f"This course now uses LLM config: {config.name}.",
            f"该课程已切换为使用 LLM 组：{config.name}。",
        ),
        "success",
    )
    return _redirect(f"/teacher/courses/{course.id}")


@router.post("/courses/{course_id}/discussion-ai-llm")
def update_course_discussion_ai_llm(
    course_id: int,
    request: Request,
    discussion_question_llm_config_id: str = Form(""),
    discussion_material_llm_config_id: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_teacher(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/teacher/courses")
    if course is None:
        return _redirect("/teacher/courses")

    from app.services.discussion_ai import parse_optional_tested_llm_config_id

    dq = parse_optional_tested_llm_config_id(db, discussion_question_llm_config_id)
    dm = parse_optional_tested_llm_config_id(db, discussion_material_llm_config_id)
    if discussion_question_llm_config_id.strip() and dq is None:
        push_flash(request, choose_text(request, "Invalid question discussion AI model.", "习题讨论 AI 模型无效或未通过测试。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}")
    if discussion_material_llm_config_id.strip() and dm is None:
        push_flash(request, choose_text(request, "Invalid material discussion AI model.", "资料讨论 AI 模型无效或未通过测试。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}")

    course.discussion_ai_question_llm_config_id = dq
    course.discussion_ai_material_llm_config_id = dm
    course.updated_at = utcnow()
    db.commit()
    push_flash(request, choose_text(request, "Discussion AI overrides saved.", "讨论区 AI 课程覆盖已保存。"), "success")
    return _redirect(f"/teacher/courses/{course_id}")


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
        push_flash(
            request,
            choose_text(request, "You do not have teacher access to this course.", "你没有该课程的教师端访问权限。"),
            "danger",
        )
        return _redirect("/teacher/courses")

    from app.models import User

    member_user = db.query(User).filter(User.username == username.strip()).first()
    if member_user is None:
        push_flash(request, choose_text(request, "User not found.", "未找到该用户。"), "danger")
        return _redirect(f"/teacher/courses/{course.id}")

    try:
        course_role = CourseRole(role)
    except ValueError:
        push_flash(request, choose_text(request, "Invalid course role.", "课程角色无效。"), "danger")
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
    push_flash(
        request,
        choose_text(
            request,
            f"Updated {member_user.username} as {course_role.value}.",
            f"已将 {member_user.username} 更新为 {course_role.value}。",
        ),
        "success",
    )
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
        push_flash(
            request,
            choose_text(request, "You do not have teacher access to this course.", "你没有该课程的教师端访问权限。"),
            "danger",
        )
        return _redirect("/teacher/courses")

    try:
        assignment_status = AssignmentStatus(status)
        scoring_rule = ScoringRule(default_scoring_rule)
        limit_mode = SubmissionLimitMode(submission_limit_mode)
    except ValueError:
        push_flash(
            request,
            choose_text(request, "Invalid assignment settings.", "作业配置无效，请检查后重试。"),
            "danger",
        )
        return _redirect(f"/teacher/courses/{course.id}")

    try:
        limit_value = (
            _parse_int_input(submission_limit_value, "Submission limit", minimum=1, maximum=1000)
            if submission_limit_value.strip()
            else None
        )
    except ValueError:
        push_flash(
            request,
            choose_text(request, "Submission limit must be a positive whole number.", "提交次数限制必须是正整数。"),
            "danger",
        )
        return _redirect(f"/teacher/courses/{course.id}")
    if limit_value is None:
        limit_mode = SubmissionLimitMode.UNLIMITED
    elif limit_mode == SubmissionLimitMode.UNLIMITED:
        limit_value = None
    elif limit_mode in {SubmissionLimitMode.DAILY, SubmissionLimitMode.TOTAL} and limit_value is None:
        limit_mode = SubmissionLimitMode.UNLIMITED

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
        submission_limit_value=limit_value,
        published_at=utcnow() if assignment_status == AssignmentStatus.PUBLISHED else None,
    )
    db.add(assignment)
    db.commit()
    push_flash(
        request,
        choose_text(request, f"Assignment {assignment.title} was created.", f"作业 {assignment.title} 已创建。"),
        "success",
    )
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
        push_flash(
            request,
            choose_text(
                request,
                "You do not have teacher access to this assignment.",
                "你没有该作业的教师端访问权限。",
            ),
            "danger",
        )
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
    student_ids = active_student_ids(db, assignment.course_id)
    assignment_staff_stats = compute_assignment_staff_stats(db, assignment.id, assignment.course_id, student_ids)
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
            "default_allowed_code_libraries": default_allowed_code_libraries_text(get_locale(request)),
            "assignment_staff_stats": assignment_staff_stats,
        },
    )


@router.post("/assignments/{assignment_id}/questions")
async def create_question(
    assignment_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    question_type: str = Form(...),
    max_score: str = Form("100"),
    scoring_rule_override: str = Form(""),
    input_spec: str = Form(""),
    output_spec: str = Form(""),
    visible_test_1_input: str = Form(""),
    visible_test_1_output: str = Form(""),
    visible_test_2_input: str = Form(""),
    visible_test_2_output: str = Form(""),
    visible_test_3_input: str = Form(""),
    visible_test_3_output: str = Form(""),
    hidden_test_1_input: str = Form(""),
    hidden_test_1_output: str = Form(""),
    hidden_test_2_input: str = Form(""),
    hidden_test_2_output: str = Form(""),
    allowed_libraries_note: str = Form(""),
    allowed_languages: list[str] = Form(["python"]),
    reference_solution_python: str = Form(""),
    reference_solution_c: str = Form(""),
    reference_solution_cpp: str = Form(""),
    time_limit_seconds: str = Form("300"),
    memory_limit_mb: str = Form("1024"),
    cpu_limit: str = Form("1"),
    rubric_text: str = Form(""),
    reference_answer: str = Form(""),
    reference_answer_file: UploadFile | None = File(None),
    accepted_extensions: str = Form(""),
    require_teacher_confirmation: str = Form("false"),
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
        push_flash(request, choose_text(request, "Invalid question type.", "题目类型无效。"), "danger")
        return _redirect(f"/teacher/assignments/{assignment.id}")

    try:
        max_score_decimal = _parse_decimal_input(max_score, "Max score")
        if max_score_decimal <= 0:
            raise ValueError
    except ValueError:
        push_flash(request, choose_text(request, "Max score must be a positive number.", "题目满分必须是正数。"), "danger")
        return _redirect(f"/teacher/assignments/{assignment.id}")
    order_index = len(assignment.questions) + 1
    teacher_confirmation_required = require_teacher_confirmation == "true"
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

    ref_file_rel: str | None = None
    if reference_answer_file and reference_answer_file.filename:
        try:
            raw = await reference_answer_file.read()
            ref_file_rel = store_reference_answer_file(
                user_id=user.id,
                question_id=question.id,
                original_filename=reference_answer_file.filename,
                file_bytes=raw,
            )
        except ValueError as exc:
            push_flash(request, choose_text(request, str(exc), str(exc)), "danger")
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")

    if q_type == QuestionType.SHORT_ANSWER:
        try:
            parsed_min_length = _parse_optional_length(min_length, "Minimum length")
            parsed_max_length = _parse_optional_length(max_length, "Maximum length")
        except ValueError:
            push_flash(request, choose_text(request, "Length limits must be whole numbers.", "字数限制必须是整数。"), "danger")
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        if parsed_min_length is not None and parsed_max_length is not None and parsed_min_length > parsed_max_length:
            push_flash(request, choose_text(request, "Minimum length cannot exceed maximum length.", "最小长度不能大于最大长度。"), "danger")
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        db.add(
            ShortAnswerQuestionConfig(
                question_id=question.id,
                min_length=parsed_min_length,
                max_length=parsed_max_length,
                rubric_text=rubric_text.strip() or None,
                llm_suggestion_enabled=True,
                teacher_confirmation_required=teacher_confirmation_required,
            )
        )
    elif q_type == QuestionType.CODE:
        normalized_languages = []
        for raw_language in allowed_languages:
            try:
                language = CodeLanguage(raw_language)
            except ValueError:
                continue
            if language not in normalized_languages:
                normalized_languages.append(language)
        if not normalized_languages:
            normalized_languages = [CodeLanguage.PYTHON]
        visible_samples = [
            {"input": visible_test_1_input.strip(), "expected_output": visible_test_1_output.strip(), "points": 20},
            {"input": visible_test_2_input.strip(), "expected_output": visible_test_2_output.strip(), "points": 20},
            {"input": visible_test_3_input.strip(), "expected_output": visible_test_3_output.strip(), "points": 20},
        ]
        hidden_samples = [
            {"input": hidden_test_1_input.strip(), "expected_output": hidden_test_1_output.strip(), "points": 20},
            {"input": hidden_test_2_input.strip(), "expected_output": hidden_test_2_output.strip(), "points": 20},
        ]
        if not input_spec.strip() or not output_spec.strip():
            push_flash(
                request,
                choose_text(
                    request,
                    "Code questions must define both input and output specifications.",
                    "代码题必须同时填写输入说明和输出说明。",
                ),
                "danger",
            )
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        if any(not sample["input"] or not sample["expected_output"] for sample in visible_samples + hidden_samples):
            push_flash(
                request,
                choose_text(
                    request,
                    "Code questions require 5 complete test cases (3 visible, 2 hidden).",
                    "代码题需要完整填写 5 个测试点（3 个可见测试，2 个隐藏测试）。",
                ),
                "danger",
            )
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        if max_score_decimal != Decimal("100"):
            push_flash(
                request,
                choose_text(
                    request,
                    "Code questions currently use a fixed 100-point rubric (5 tests x 20 points).",
                    "当前代码题固定按 100 分计分（5 个测试点，每个 20 分）。",
                ),
                "warning",
            )
            question.max_score = Decimal("100")
        try:
            parsed_time_limit, parsed_memory_limit, parsed_cpu_limit = _parse_code_runner_limits(
                time_limit_seconds,
                memory_limit_mb,
                cpu_limit,
            )
        except ValueError as exc:
            push_flash(request, choose_text(request, str(exc), "运行资源限制无效，请检查时间、内存和 CPU。"), "danger")
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        db.add(
            CodeQuestionConfig(
                question_id=question.id,
                input_spec=input_spec.strip(),
                output_spec=output_spec.strip(),
                visible_tests_json=json.dumps(visible_samples, ensure_ascii=True, indent=2),
                hidden_tests_json=json.dumps(hidden_samples, ensure_ascii=True, indent=2),
                allowed_libraries_note=allowed_libraries_note.strip()
                or DEFAULT_ALLOWED_CODE_LIBRARIES,
                allowed_languages_json=json.dumps([language.value for language in normalized_languages], ensure_ascii=True),
                reference_solution_python=reference_solution_python.strip(),
                reference_solution_c=reference_solution_c.strip(),
                reference_solution_cpp=reference_solution_cpp.strip(),
                time_limit_seconds=parsed_time_limit,
                memory_limit_mb=parsed_memory_limit,
                cpu_limit=parsed_cpu_limit,
                allow_network=False,
            )
        )
    elif q_type == QuestionType.FILE_LLM:
        normalized_extensions = [
            ext.strip().lower() if ext.strip().lower().startswith(".") else f".{ext.strip().lower()}"
            for ext in (accepted_extensions or "").replace(" ", "").split(",")
            if ext.strip()
        ]
        if not normalized_extensions:
            push_flash(
                request,
                choose_text(request, "Select at least one allowed file format.", "请至少选择一种允许提交的文件格式。"),
                "danger",
            )
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        if not rubric_text.strip() or (not reference_answer.strip() and not ref_file_rel):
            push_flash(
                request,
                choose_text(
                    request,
                    "Rubric is required, and you must provide a reference answer (text and/or upload).",
                    "必须填写评分细则，并提供参考答案（文本和/或上传附件）。",
                ),
                "danger",
            )
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        notebook_outputs_required = ".ipynb" in set(normalized_extensions)
        db.add(
            FileQuestionConfig(
                question_id=question.id,
                accepted_extensions=",".join(normalized_extensions),
                rubric_text=rubric_text.strip(),
                reference_answer_text=reference_answer.strip(),
                reference_answer_file_path=ref_file_rel,
                llm_suggestion_enabled=True,
                teacher_confirmation_required=teacher_confirmation_required,
                notebook_outputs_required=notebook_outputs_required,
                updated_at=utcnow(),
            )
        )

    db.commit()
    db.refresh(question)
    create_initial_question_version(db, question)
    db.commit()
    push_flash(
        request,
        choose_text(request, f"Question {question.title} was created.", f"题目 {question.title} 已创建。"),
        "success",
    )
    return _redirect(f"/teacher/questions/{question.id}")


@router.get("/questions/{question_id}")
def teacher_question_detail(question_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        question = get_question_for_staff(db, question_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(
            request,
            choose_text(request, "You do not have teacher access to this question.", "你没有该题目的教师端访问权限。"),
            "danger",
        )
        return _redirect("/teacher/courses")

    if question is None:
        push_flash(
            request,
            choose_text(request, "You do not have teacher access to this question.", "你没有该题目的教师端访问权限。"),
            "danger",
        )
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
    versions = (
        db.query(QuestionVersion)
        .filter(QuestionVersion.question_id == question.id)
        .order_by(QuestionVersion.version_number.desc())
        .all()
    )
    sid_list = active_student_ids(db, question.assignment.course_id)
    question_class_stats = compute_question_class_stats(db, question.id, question.assignment.course_id, sid_list)
    topic = get_or_create_question_topic(db, question.id, question.assignment.course_id)
    db.commit()
    disc_ctx = build_discussion_view_context(
        db,
        topic_id=topic.id,
        course_id=question.assignment.course_id,
        viewer=user,
        request=request,
    )
    reveal = reveal_bundle_for_question(db, question)
    can_discuss = can_post_on_question_topic(db, question, user)
    rq = getattr(request.url, "query", "") or ""
    disc_ret = f"{request.url.path}?{rq}" if rq else request.url.path
    cid = question.assignment.course_id
    return render_template(
        request,
        db,
        "teacher_question_detail.html",
        {
            "question": question,
            "submissions": submissions,
            "snapshots": snapshots,
            "versions": versions,
            "course_role": course_role,
            "can_manage_course": course_role == CourseRole.TEACHER,
            "default_allowed_code_libraries": default_allowed_code_libraries_text(get_locale(request)),
            "question_class_stats": question_class_stats,
            "topic_id": topic.id,
            **disc_ctx,
            "can_post_discussion": can_discuss,
            "reveal": reveal,
            "discussion_post_url": f"/teacher/questions/{question_id}/discuss",
            "discussion_notice": "",
            "discussion_redirect_to": disc_ret,
            "discussion_delete_action": f"/teacher/courses/{cid}/discussion/delete-post",
        },
    )


@router.post("/questions/{question_id}/discuss")
async def teacher_question_discuss(
    question_id: int,
    request: Request,
    body: str = Form(""),
    parent_post_id: str = Form(""),
    anonymous: str = Form(""),
    request_ai: str = Form(""),
    ai_group_id: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        question = get_question_for_staff(db, question_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if question is None:
        return _redirect("/teacher/courses")
    if not can_post_on_question_topic(db, question, user):
        push_flash(request, choose_text(request, "You cannot post here.", "你无法在此发言。"), "danger")
        return _redirect(f"/teacher/questions/{question_id}")
    topic = get_or_create_question_topic(db, question.id, question.assignment.course_id)
    db.commit()
    pid = int(parent_post_id) if parent_post_id.strip().isdigit() else None
    selected_group_id = int(ai_group_id) if ai_group_id.strip().isdigit() else None
    image_files = await extract_discussion_images(request)
    attachment_paths: list[str] = []
    default_dest = f"/teacher/questions/{question_id}"
    try:
        _u, ai_err = create_user_post_and_maybe_ai_reply(
            db,
            topic=topic,
            user=user,
            body=body,
            parent_post_id=pid,
            is_anonymous=(anonymous == "on" or anonymous == "true"),
            request_ai=(request_ai == "on" or request_ai == "true"),
            pending_image_uploads=bool(image_files),
            selected_llm_group_id=selected_group_id,
        )
        if _u is not None and image_files:
            try:
                attachment_paths = attach_discussion_images_to_post(
                    db, _u, question.assignment.course_id, image_files
                )
            except ValueError as att_err:
                ak = str(att_err) if att_err else ""
                if ak == "too_many_images":
                    raise ValueError("too_many_images") from att_err
                if ak == "storage_quota_exceeded":
                    raise ValueError("storage_quota_exceeded") from att_err
                if ak in ("unsupported_image_type", "file_too_large"):
                    raise ValueError(ak) from att_err
                raise
        db.commit()
    except ValueError as exc:
        db.rollback()
        delete_discussion_attachment_files(attachment_paths)
        key = str(exc) if exc else ""
        if key == "user_muted":
            msg = choose_text(
                request,
                "You are muted from posting in this course discussion.",
                "你已被禁止在本课程讨论区发言。",
            )
        elif key == "body_too_large":
            msg = choose_text(request, "Message is too long.", "内容过长。")
        elif key == "remote_images_not_allowed":
            msg = choose_text(
                request,
                "Remote images in markdown are not allowed; use uploads instead.",
                "不允许在 Markdown 中嵌入外链图片，请使用上传图片。",
            )
        elif key == "too_many_images":
            msg = choose_text(request, "Too many images for one post.", "单条帖子图片数量超过上限。")
        elif key == "storage_quota_exceeded":
            msg = choose_text(request, "Storage quota exceeded.", "存储空间已满，无法上传图片。")
        elif key in ("unsupported_image_type", "file_too_large"):
            msg = format_image_upload_error(request, key)
        else:
            msg = choose_text(request, "Message cannot be empty.", "内容不能为空。")
        dest = safe_local_redirect(redirect_to, default_dest)
        push_flash(request, msg, "danger")
        return _redirect(dest)
    except Exception:
        db.rollback()
        delete_discussion_attachment_files(attachment_paths)
        raise
    push_flash(request, choose_text(request, "Posted.", "已发布。"), "success")
    if ai_err:
        push_flash(request, choose_text(request, f"AI: {ai_err}", f"AI：{ai_err}"), "warning")
    dest = safe_local_redirect(redirect_to, default_dest)
    return _redirect(dest)


def _discussion_post_in_course(db: Session, post_id: int, course_id: int) -> DiscussionPost | None:
    post = db.get(DiscussionPost, post_id)
    if post is None or getattr(post, "deleted_at", None) is not None:
        return None
    topic = db.get(DiscussionTopic, post.topic_id)
    if topic is None or topic.course_id != course_id:
        return None
    return post


@router.post("/courses/{course_id}/discussion/delete-post")
def teacher_delete_discussion_post(
    course_id: int,
    request: Request,
    post_id: int = Form(...),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None:
        return _redirect("/teacher/courses")
    if not can_moderate_discussion(db, course_id, user):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}")
    post = _discussion_post_in_course(db, post_id, course_id)
    if post is None:
        push_flash(request, choose_text(request, "Post not found.", "未找到帖子。"), "danger")
        return _redirect(redirect_to or f"/teacher/courses/{course_id}")
    hard_delete_post(db, post, actor=user, course_id=course_id)
    db.commit()
    push_flash(request, choose_text(request, "Post deleted.", "帖子已删除。"), "success")
    return _redirect(redirect_to or f"/teacher/courses/{course_id}")


@router.post("/courses/{course_id}/discussion/mute-user")
def teacher_mute_discussion_user(
    course_id: int,
    request: Request,
    user_id: int = Form(...),
    muted_until: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None:
        return _redirect("/teacher/courses")
    if not can_moderate_discussion(db, course_id, user):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}")
    target = db.get(User, user_id)
    if target is None:
        push_flash(request, choose_text(request, "User not found.", "用户不存在。"), "danger")
        return _redirect(redirect_to or f"/teacher/courses/{course_id}")
    until_dt = None
    raw = (muted_until or "").strip()
    if raw:
        try:
            until_dt = datetime.fromisoformat(raw)
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=display_timezone)
        except ValueError:
            push_flash(request, choose_text(request, "Invalid end time.", "结束时间无效。"), "danger")
            return _redirect(redirect_to or f"/teacher/courses/{course_id}")
    mute_user_in_course(db, course_id=course_id, target_user_id=target.id, actor=user, muted_until=until_dt)
    db.commit()
    push_flash(request, choose_text(request, "User muted from discussions.", "已禁止该用户在本课程讨论区发言。"), "success")
    return _redirect(redirect_to or f"/teacher/courses/{course_id}")


@router.post("/courses/{course_id}/discussion/unmute-user")
def teacher_unmute_discussion_user(
    course_id: int,
    request: Request,
    user_id: int = Form(...),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None:
        return _redirect("/teacher/courses")
    if not can_moderate_discussion(db, course_id, user):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}")
    unmute_user_in_course(db, course_id=course_id, target_user_id=user_id, actor=user)
    db.commit()
    push_flash(request, choose_text(request, "Mute removed.", "已解除禁言。"), "success")
    return _redirect(redirect_to or f"/teacher/courses/{course_id}")


@router.get("/courses/{course_id}/grades/student/{student_id}")
def teacher_student_course_grades(course_id: int, student_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(request, choose_text(request, "You do not have access to this course.", "你没有该课程的访问权限。"), "danger")
        return _redirect("/teacher/courses")
    if course is None:
        return _redirect("/teacher/courses")
    student = db.get(User, student_id)
    if student is None:
        return _redirect(f"/teacher/courses/{course_id}")
    membership = (
        db.query(CourseMember)
        .filter(
            CourseMember.course_id == course_id,
            CourseMember.user_id == student_id,
            CourseMember.role == CourseRole.STUDENT,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
        .first()
    )
    if membership is None:
        push_flash(
            request,
            choose_text(request, "That user is not an active student in this course.", "该用户不是本课程的活跃学生。"),
            "danger",
        )
        return _redirect(f"/teacher/courses/{course_id}")

    assignments = (
        db.query(Assignment).filter(Assignment.course_id == course_id).order_by(Assignment.created_at.desc()).all()
    )
    all_student_ids = active_student_ids(db, course_id)
    rows = []
    for asn in assignments:
        questions = (
            db.query(Question).filter(Question.assignment_id == asn.id).order_by(Question.order_index.asc()).all()
        )
        q_ids = [q.id for q in questions]
        class_scores_by_q = score_distribution_by_question(db, q_ids, all_student_ids)
        q_cells = []
        for q in questions:
            sub = (
                db.query(Submission)
                .filter(Submission.question_id == q.id, Submission.user_id == student_id)
                .order_by(Submission.submitted_at.desc())
                .first()
            )
            snap = (
                db.query(FinalGradeSnapshot)
                .filter(
                    FinalGradeSnapshot.question_id == q.id,
                    FinalGradeSnapshot.student_id == student_id,
                )
                .first()
            )
            peer_scores = class_scores_by_q.get(q.id, [])
            st_score = float(snap.score) if snap is not None and snap.score is not None else None
            q_cells.append(
                {
                    "question": q,
                    "submitted": sub is not None,
                    "submission": sub,
                    "snapshot": snap,
                    "class_score_summary": grade_summary_from_float_scores(peer_scores),
                    "percentile_rank": percentile_rank(peer_scores, st_score),
                }
            )
        asn_total = (
            db.scalar(
                select(func.coalesce(func.sum(FinalGradeSnapshot.score), 0)).where(
                    FinalGradeSnapshot.assignment_id == asn.id,
                    FinalGradeSnapshot.student_id == student_id,
                )
            )
            or 0
        )
        rows.append({"assignment": asn, "questions": q_cells, "assignment_total": asn_total})
    return render_template(
        request,
        db,
        "teacher_student_course_grades.html",
        {"course": course, "student": student, "rows": rows},
    )


@router.post("/questions/{question_id}/historical-highest")
def apply_historical_highest_grading(question_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        question = get_question_for_teacher(db, question_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/teacher/courses")
    if question is None:
        push_flash(request, choose_text(request, "Question not found.", "题目不存在。"), "danger")
        return _redirect("/teacher/courses")

    for snap in db.query(FinalGradeSnapshot).filter(FinalGradeSnapshot.question_id == question.id).all():
        snap.use_historical_highest = True
    db.commit()
    student_ids = list(
        db.scalars(select(Submission.user_id).where(Submission.question_id == question.id).distinct()).all()
    )
    for uid in student_ids:
        latest = (
            db.query(Submission)
            .filter(Submission.question_id == question.id, Submission.user_id == uid)
            .order_by(Submission.submitted_at.desc())
            .first()
        )
        if latest is not None:
            refresh_final_grade_snapshot(db, question.id, uid)
    push_flash(
        request,
        choose_text(
            request,
            "Historical highest scoring is now enabled for all students on this question.",
            "已为本题所有学生启用按历史最高分计分。",
        ),
        "success",
    )
    return _redirect(f"/teacher/questions/{question.id}")


@router.post("/questions/{question_id}/update")
async def update_question(
    question_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    max_score: str = Form("100"),
    scoring_rule_override: str = Form(""),
    input_spec: str = Form(""),
    output_spec: str = Form(""),
    visible_test_1_input: str = Form(""),
    visible_test_1_output: str = Form(""),
    visible_test_2_input: str = Form(""),
    visible_test_2_output: str = Form(""),
    visible_test_3_input: str = Form(""),
    visible_test_3_output: str = Form(""),
    hidden_test_1_input: str = Form(""),
    hidden_test_1_output: str = Form(""),
    hidden_test_2_input: str = Form(""),
    hidden_test_2_output: str = Form(""),
    allowed_libraries_note: str = Form(""),
    allowed_languages: list[str] = Form(["python"]),
    reference_solution_python: str = Form(""),
    reference_solution_c: str = Form(""),
    reference_solution_cpp: str = Form(""),
    time_limit_seconds: str = Form("300"),
    memory_limit_mb: str = Form("1024"),
    cpu_limit: str = Form("1"),
    rubric_text: str = Form(""),
    reference_answer: str = Form(""),
    reference_answer_file: UploadFile | None = File(None),
    clear_reference_answer_file: str = Form("false"),
    accepted_extensions: str = Form(""),
    require_teacher_confirmation: str = Form("false"),
    min_length: str = Form(""),
    max_length: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        question = get_question_for_teacher(db, question_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/teacher/courses")
    if question is None:
        push_flash(request, choose_text(request, "Question not found.", "题目不存在。"), "danger")
        return _redirect("/teacher/courses")

    try:
        max_score_decimal = _parse_decimal_input(max_score, "Max score")
        if max_score_decimal <= 0:
            raise ValueError
        scoring_rule_value = ScoringRule(scoring_rule_override) if scoring_rule_override.strip() else None
    except ValueError:
        push_flash(request, choose_text(request, "Question settings are invalid.", "题目配置无效，请检查后重试。"), "danger")
        return _redirect(f"/teacher/questions/{question.id}")

    question.title = title.strip()
    question.description = description.strip() or None
    question.max_score = max_score_decimal
    question.scoring_rule_override = scoring_rule_value
    question.updated_at = utcnow()
    teacher_confirmation_required = require_teacher_confirmation == "true"

    new_reference_file_path: str | None = None
    uploaded_ref = False
    if reference_answer_file and reference_answer_file.filename:
        try:
            raw = await reference_answer_file.read()
            new_reference_file_path = store_reference_answer_file(
                user_id=user.id,
                question_id=question.id,
                original_filename=reference_answer_file.filename,
                file_bytes=raw,
            )
            uploaded_ref = True
        except ValueError as exc:
            push_flash(request, choose_text(request, str(exc), str(exc)), "danger")
            return _redirect(f"/teacher/questions/{question.id}")

    if question.question_type == QuestionType.SHORT_ANSWER and question.short_answer_config:
        cfg = question.short_answer_config
        try:
            cfg.min_length = _parse_optional_length(min_length, "Minimum length")
            cfg.max_length = _parse_optional_length(max_length, "Maximum length")
        except ValueError:
            push_flash(request, choose_text(request, "Length limits must be whole numbers.", "字数限制必须是整数。"), "danger")
            return _redirect(f"/teacher/questions/{question.id}")
        if cfg.min_length is not None and cfg.max_length is not None and cfg.min_length > cfg.max_length:
            push_flash(request, choose_text(request, "Minimum length cannot exceed maximum length.", "最小长度不能大于最大长度。"), "danger")
            return _redirect(f"/teacher/questions/{question.id}")
        cfg.rubric_text = rubric_text.strip() or None
        cfg.teacher_confirmation_required = teacher_confirmation_required
        cfg.updated_at = utcnow()
    elif question.question_type == QuestionType.CODE and question.code_config:
        cfg = question.code_config
        normalized_languages = []
        for raw_language in allowed_languages:
            try:
                language = CodeLanguage(raw_language)
            except ValueError:
                continue
            if language not in normalized_languages:
                normalized_languages.append(language)
        if not normalized_languages:
            normalized_languages = [CodeLanguage.PYTHON]
        visible_samples = [
            {"input": visible_test_1_input.strip(), "expected_output": visible_test_1_output.strip(), "points": 20},
            {"input": visible_test_2_input.strip(), "expected_output": visible_test_2_output.strip(), "points": 20},
            {"input": visible_test_3_input.strip(), "expected_output": visible_test_3_output.strip(), "points": 20},
        ]
        hidden_samples = [
            {"input": hidden_test_1_input.strip(), "expected_output": hidden_test_1_output.strip(), "points": 20},
            {"input": hidden_test_2_input.strip(), "expected_output": hidden_test_2_output.strip(), "points": 20},
        ]
        cfg.input_spec = input_spec.strip()
        cfg.output_spec = output_spec.strip()
        cfg.visible_tests_json = json.dumps(visible_samples, ensure_ascii=True, indent=2)
        cfg.hidden_tests_json = json.dumps(hidden_samples, ensure_ascii=True, indent=2)
        cfg.allowed_libraries_note = allowed_libraries_note.strip() or DEFAULT_ALLOWED_CODE_LIBRARIES
        cfg.allowed_languages_json = json.dumps([language.value for language in normalized_languages], ensure_ascii=True)
        cfg.reference_solution_python = reference_solution_python.strip()
        cfg.reference_solution_c = reference_solution_c.strip()
        cfg.reference_solution_cpp = reference_solution_cpp.strip()
        try:
            cfg.time_limit_seconds, cfg.memory_limit_mb, cfg.cpu_limit = _parse_code_runner_limits(
                time_limit_seconds,
                memory_limit_mb,
                cpu_limit,
            )
        except ValueError as exc:
            push_flash(request, choose_text(request, str(exc), "运行资源限制无效，请检查时间、内存和 CPU。"), "danger")
            return _redirect(f"/teacher/questions/{question.id}")
        cfg.allow_network = False
        cfg.updated_at = utcnow()
    elif question.file_question_config:
        cfg = question.file_question_config
        if question.question_type == QuestionType.FILE_LLM:
            normalized = [
                ext.strip().lower() if ext.strip().lower().startswith(".") else f".{ext.strip().lower()}"
                for ext in (accepted_extensions or "").replace(" ", "").split(",")
                if ext.strip()
            ]
            if not normalized:
                push_flash(
                    request,
                    choose_text(request, "Select at least one allowed file format.", "请至少选择一种允许提交的文件格式。"),
                    "danger",
                )
                return _redirect(f"/teacher/questions/{question.id}")
            cfg.accepted_extensions = ",".join(normalized)
            cfg.notebook_outputs_required = ".ipynb" in set(normalized)
        cfg.rubric_text = rubric_text.strip()
        cfg.reference_answer_text = reference_answer.strip()
        if clear_reference_answer_file == "true":
            cfg.reference_answer_file_path = None
        elif uploaded_ref:
            cfg.reference_answer_file_path = new_reference_file_path
        cfg.teacher_confirmation_required = teacher_confirmation_required
        cfg.updated_at = utcnow()

    db.commit()
    append_question_version_after_edit(db, question)
    db.commit()
    student_ids = list(
        db.scalars(select(Submission.user_id).where(Submission.question_id == question.id).distinct()).all()
    )
    for uid in student_ids:
        latest = (
            db.query(Submission)
            .filter(Submission.question_id == question.id, Submission.user_id == uid)
            .order_by(Submission.submitted_at.desc())
            .first()
        )
        if latest is not None:
            refresh_final_grade_snapshot(db, question.id, uid)
    push_flash(request, choose_text(request, "Question was updated.", "题目已更新。"), "success")
    return _redirect(f"/teacher/questions/{question.id}")


@router.post("/submissions/{submission_id}/purge-files")
def teacher_purge_submission_files(submission_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        submission = get_submission_for_teacher(db, submission_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(
            request,
            choose_text(
                request,
                "You do not have teacher access to this submission.",
                "你没有该提交记录的教师端访问权限。",
            ),
            "danger",
        )
        return _redirect("/teacher/courses")
    if get_course_role(db, submission.course_id, user.id) != CourseRole.TEACHER:
        push_flash(
            request,
            choose_text(request, "Only teachers can remove files for this course.", "只有本课程教师可以删除学生提交文件。"),
            "danger",
        )
        return _redirect(f"/teacher/submissions/{submission.id}")
    err = purge_submission_as_viewer(db, user, submission)
    if err == "not_found":
        push_flash(request, choose_text(request, "No file stored for this submission.", "该提交没有可删除的文件。"), "warning")
    elif err == "forbidden":
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
    else:
        push_flash(
            request,
            choose_text(
                request,
                "Stored files were removed. Grades and feedback are unchanged.",
                "已删除服务器上的提交文件，成绩与反馈保持不变。",
            ),
            "success",
        )
    return _redirect(f"/teacher/submissions/{submission_id}")


@router.get("/submissions/{submission_id}")
def teacher_submission_detail(submission_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        submission = get_submission_for_teacher(db, submission_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        push_flash(
            request,
            choose_text(
                request,
                "You do not have teacher access to this submission.",
                "你没有该提交记录的教师端访问权限。",
            ),
            "danger",
        )
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
        push_flash(
            request,
            choose_text(
                request,
                "You do not have teacher access to this submission.",
                "你没有该提交记录的教师端访问权限。",
            ),
            "danger",
        )
        return _redirect("/teacher/courses")

    if get_course_role(db, submission.course_id, user.id) != CourseRole.TEACHER:
        push_flash(
            request,
            choose_text(
                request,
                "Only teachers can save final grading decisions for this course.",
                "只有教师角色可以保存该课程的最终评分结论。",
            ),
            "danger",
        )
        return _redirect(f"/teacher/submissions/{submission.id}")

    try:
        score_value = _parse_decimal_input(score, "Score") if score.strip() else None
    except ValueError:
        push_flash(request, choose_text(request, "Score must be a valid number.", "分数必须是有效数字。"), "danger")
        return _redirect(f"/teacher/submissions/{submission.id}")
    requires_teacher_score = submission.submission_type == QuestionType.SHORT_ANSWER
    if requires_teacher_score and score_value is None:
        push_flash(
            request,
            choose_text(
                request,
                "A score is required when saving teacher feedback.",
                "保存教师反馈时必须填写分数。",
            ),
            "danger",
        )
        return _redirect(f"/teacher/submissions/{submission.id}")
    if score_value is not None and (score_value < 0 or score_value > Decimal(str(submission.question.max_score))):
        push_flash(
            request,
            choose_text(
                request,
                f"Score must be between 0 and {submission.question.max_score}.",
                f"分数必须在 0 到 {submission.question.max_score} 之间。",
            ),
            "danger",
        )
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
    push_flash(request, choose_text(request, "Teacher feedback was saved.", "教师反馈已保存。"), "success")
    return _redirect(f"/teacher/submissions/{submission.id}")


def _parse_datetime_input(value: str):
    value = value.strip()
    if not value:
        return None
    normalized = value.replace("T", " ")
    try:
        local_value = datetime.fromisoformat(normalized)
        if local_value.tzinfo is None:
            local_value = local_value.replace(tzinfo=display_timezone)
        return local_value.astimezone(ZoneInfo("UTC"))
    except ValueError:
        return None
