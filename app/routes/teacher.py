import json
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import push_flash
from app.constants import (
    AssignmentStatus,
    CodeLanguage,
    CourseRole,
    FeedbackSource,
    LLMResponseLanguage,
    LLMTestStatus,
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
    Feedback,
    FinalGradeSnapshot,
    FileQuestionConfig,
    LLMConfig,
    NotebookQuestionConfig,
    CodeQuestionConfig,
    Question,
    QuestionVersion,
    ShortAnswerQuestionConfig,
    Submission,
    User,
)
from app.runtime_support import default_allowed_code_libraries_text
from app.services.courses import (
    DEFAULT_ALLOWED_CODE_LIBRARIES,
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
from app.services.question_versions import append_question_version_after_edit, create_initial_question_version
from app.services.submissions import (
    get_submission_for_teacher,
    is_submission_pending_teacher_review,
    read_submission_artifact_text,
    refresh_final_grade_snapshot,
    store_reference_answer_file,
)
from app.web import render_template


router = APIRouter(prefix="/teacher", tags=["teacher"])
settings = get_settings()
display_timezone = ZoneInfo(settings.timezone_name)


_TEACHER_UPLOAD_ERROR_ZH = {
    "Reference answer uploads must be one of:": "参考答案附件格式不支持。请上传 PDF、TeX、TXT、Markdown 或 ipynb 文件。",
}


def _teacher_error_message(request: Request, exc: Exception) -> str:
    text = str(exc)
    zh = next((message for prefix, message in _TEACHER_UPLOAD_ERROR_ZH.items() if text.startswith(prefix)), text)
    return choose_text(request, text, zh)


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
        push_flash(
            request,
            choose_text(request, "Course code and title are required.", "课程编号和课程标题不能为空。"),
            "danger",
        )
        return _redirect("/teacher/courses")

    if db.query(Course).filter(Course.code == code).first():
        push_flash(request, choose_text(request, "Course code already exists.", "课程编号已存在。"), "danger")
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
        .filter(
            LLMConfig.enabled.is_(True),
            LLMConfig.scope == "platform",
            LLMConfig.last_test_status == LLMTestStatus.SUCCESS,
        )
        .order_by(LLMConfig.last_tested_at.desc(), LLMConfig.created_at.desc())
        .all()
    )
    course_role = get_course_role(db, course.id, user.id)
    grade_matrix = summarize_course_grade_matrix(db, course.id) if course_role == CourseRole.TEACHER else None
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
            "available_llm_configs": available_llm_configs,
            "grade_matrix": grade_matrix,
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
    if config is None or not config.enabled or config.last_test_status != LLMTestStatus.SUCCESS:
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
            f"该课程已切换为使用 LLM 配置：{config.name}。",
        ),
        "success",
    )
    return _redirect(f"/teacher/courses/{course.id}")


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

    limit_value = int(submission_limit_value) if submission_limit_value.strip() else None
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
    allow_network: str = Form("false"),
    visible_tests_source: str = Form(""),
    hidden_tests_source: str = Form(""),
    execution_weight: str = Form("0"),
    visible_weight: str = Form("100"),
    hidden_weight: str = Form("0"),
    llm_score_weight: str = Form("0"),
    llm_scoring_rubric: str = Form(""),
    llm_feedback_enabled: str = Form("false"),
    notebook_llm_score_weight: str = Form("20"),
    notebook_llm_feedback_enabled: str = Form("true"),
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
        push_flash(
            request,
            choose_text(request, "You do not have teacher access to this assignment.", "你没有该作业的教师端访问权限。"),
            "danger",
        )
        return _redirect("/teacher/courses")

    try:
        q_type = QuestionType(question_type)
    except ValueError:
        push_flash(request, choose_text(request, "Invalid question type.", "题目类型无效。"), "danger")
        return _redirect(f"/teacher/assignments/{assignment.id}")

    max_score_decimal = Decimal(max_score)
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
            push_flash(request, _teacher_error_message(request, exc), "danger")
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")

    if q_type == QuestionType.NOTEBOOK:
        rubric = llm_scoring_rubric.strip() or rubric_text.strip()
        if not rubric:
            push_flash(
                request,
                choose_text(request, "Notebook LLM questions require an LLM scoring rubric.", "Notebook LLM 题必须填写 LLM 评分细则。"),
                "danger",
            )
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        n_weight = Decimal(notebook_llm_score_weight or "0")
        if n_weight <= 0 or notebook_llm_feedback_enabled != "true":
            push_flash(
                request,
                choose_text(
                    request,
                    "Notebook LLM questions require a positive LLM score weight and LLM feedback enabled.",
                    "Notebook LLM 题需要填写大于 0 的 LLM 分数权重并启用 LLM 反馈。",
                ),
                "danger",
            )
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        if n_weight > max_score_decimal:
            push_flash(
                request,
                choose_text(request, "LLM score weight cannot exceed the question max score.", "LLM 分数权重不能超过题目满分。"),
                "danger",
            )
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        if not reference_answer.strip() and not ref_file_rel:
            push_flash(
                request,
                choose_text(
                    request,
                    "Provide a reference answer (text and/or upload) for notebook LLM grading.",
                    "请为 Notebook LLM 评阅提供参考答案（文本和/或上传附件）。",
                ),
                "danger",
            )
            db.rollback()
            return _redirect(f"/teacher/assignments/{assignment.id}")
        db.add(
            NotebookQuestionConfig(
                question_id=question.id,
                time_limit_seconds=300,
                memory_limit_mb=1024,
                cpu_limit="1",
                allow_network=False,
                execution_weight=Decimal("0"),
                visible_weight=Decimal("100"),
                hidden_weight=Decimal("0"),
                llm_score_weight=n_weight,
                llm_scoring_rubric=rubric,
                llm_feedback_enabled=True,
                reference_answer_text=reference_answer.strip(),
                reference_answer_file_path=ref_file_rel,
            )
        )
    elif q_type == QuestionType.SHORT_ANSWER:
        db.add(
            ShortAnswerQuestionConfig(
                question_id=question.id,
                min_length=int(min_length) if min_length.strip() else None,
                max_length=int(max_length) if max_length.strip() else None,
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
                time_limit_seconds=int(time_limit_seconds or 300),
                memory_limit_mb=int(memory_limit_mb or 1024),
                cpu_limit=cpu_limit or "1",
                allow_network=allow_network == "true",
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
    elif q_type in {QuestionType.PDF_LLM, QuestionType.FORMATTED_TEXT_LLM}:
        normalized_extensions = [
            ext.strip().lower()
            for ext in (accepted_extensions or ".txt,.tex,.ipynb").split(",")
            if ext.strip()
        ]
        if q_type == QuestionType.PDF_LLM:
            normalized_extensions = [".pdf"]
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
        db.add(
            FileQuestionConfig(
                question_id=question.id,
                accepted_extensions=",".join(normalized_extensions),
                rubric_text=rubric_text.strip(),
                reference_answer_text=reference_answer.strip(),
                reference_answer_file_path=ref_file_rel,
                llm_suggestion_enabled=True,
                teacher_confirmation_required=teacher_confirmation_required,
                notebook_outputs_required=q_type == QuestionType.FORMATTED_TEXT_LLM,
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
        },
    )


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
    rows = []
    for asn in assignments:
        questions = (
            db.query(Question).filter(Question.assignment_id == asn.id).order_by(Question.order_index.asc()).all()
        )
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
            q_cells.append(
                {
                    "question": q,
                    "submitted": sub is not None,
                    "submission": sub,
                    "snapshot": snap,
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
    allow_network: str = Form("false"),
    rubric_text: str = Form(""),
    reference_answer: str = Form(""),
    reference_answer_file: UploadFile | None = File(None),
    clear_reference_answer_file: str = Form("false"),
    accepted_extensions: str = Form(""),
    require_teacher_confirmation: str = Form("false"),
    min_length: str = Form(""),
    max_length: str = Form(""),
    notebook_llm_score_weight: str = Form(""),
    notebook_llm_rubric: str = Form(""),
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

    question.title = title.strip()
    question.description = description.strip() or None
    question.max_score = Decimal(max_score)
    question.scoring_rule_override = ScoringRule(scoring_rule_override) if scoring_rule_override.strip() else None
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
            push_flash(request, _teacher_error_message(request, exc), "danger")
            return _redirect(f"/teacher/questions/{question.id}")

    if question.question_type == QuestionType.SHORT_ANSWER and question.short_answer_config:
        cfg = question.short_answer_config
        cfg.min_length = int(min_length) if min_length.strip() else None
        cfg.max_length = int(max_length) if max_length.strip() else None
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
        cfg.time_limit_seconds = int(time_limit_seconds or 300)
        cfg.memory_limit_mb = int(memory_limit_mb or 1024)
        cfg.cpu_limit = cpu_limit or "1"
        cfg.allow_network = allow_network == "true"
        cfg.updated_at = utcnow()
    elif question.question_type == QuestionType.NOTEBOOK and question.notebook_config:
        cfg = question.notebook_config
        if notebook_llm_score_weight.strip():
            nw = Decimal(notebook_llm_score_weight.strip())
            if nw <= 0 or nw > question.max_score:
                push_flash(
                    request,
                    choose_text(request, "Invalid LLM score weight.", "LLM 分数权重无效。"),
                    "danger",
                )
                return _redirect(f"/teacher/questions/{question.id}")
            cfg.llm_score_weight = nw
        if notebook_llm_rubric.strip():
            cfg.llm_scoring_rubric = notebook_llm_rubric.strip()
        cfg.reference_answer_text = reference_answer.strip()
        if clear_reference_answer_file == "true":
            cfg.reference_answer_file_path = None
        elif uploaded_ref:
            cfg.reference_answer_file_path = new_reference_file_path
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

    score_value = Decimal(score) if score.strip() else None
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
