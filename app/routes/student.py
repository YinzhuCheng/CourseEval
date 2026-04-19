from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import push_flash
from app.constants import CodeLanguage, QuestionType
from app.db import get_db
from app.i18n import choose_text
from app.runtime_support import (
    SUPPORTED_PYTHON_PACKAGES,
    SUPPORTED_PYTHON_VERSION,
    UNSUPPORTED_PACKAGE_NOTE_EN,
    UNSUPPORTED_PACKAGE_NOTE_ZH,
    default_allowed_code_libraries_text,
)
from app.services.courses import (
    get_assignment_for_student,
    get_course_for_student,
    get_question_for_student,
    join_course_by_code,
    list_courses_for_student,
)
from app.services.permissions import RedirectRequired, require_student_access, require_user
from app.services.llm_token_usage import usage_summary_for_user
from app.services.submissions import (
    build_student_result_view,
    create_file_submission,
    create_notebook_submission,
    create_code_submission,
    create_short_answer_submission,
    enqueue_submission_evaluation,
    get_submission_for_student,
    is_submission_pending_teacher_review,
    list_submissions_for_question,
    read_student_safe_submission_artifact_text,
)
from app.web import render_template


router = APIRouter(prefix="/student", tags=["student"])


_SUBMISSION_ERROR_ZH = {
    "File type": "文件类型不符合该题要求。",
    "The uploaded PDF is empty.": "上传的 PDF 是空文件。",
    "The uploaded PDF could not be rendered into images.": "上传的 PDF 无法渲染为页面图片。",
    "Notebook submissions for this question must include executed outputs before upload.": "该题要求上传前已执行过的 Notebook，并保留输出结果。",
    "The uploaded notebook is empty.": "上传的 Notebook 没有可读取内容。",
    "Answer cannot be empty.": "作答内容不能为空。",
    "Unsupported code language.": "不支持所选代码语言。",
    "Code question configuration is missing for this question.": "该代码题配置不完整，请联系教师。",
    "This language is not allowed for the question.": "该题不允许使用所选语言。",
    "Uploaded code file is empty.": "上传的代码文件为空。",
    "File question configuration is missing for this question.": "该文件题配置不完整，请联系教师。",
    "The uploaded file does not contain any extractable content.": "上传文件没有可读取内容。",
    "Submission window has not opened yet.": "提交尚未开放。",
    "Submission window is closed.": "提交已关闭。",
    "Late submissions are not allowed for this assignment.": "该作业不允许迟交。",
    "Submission limit reached.": "已达到提交次数限制。",
}


def _submission_error_message(request: Request, exc: Exception) -> str:
    text = str(exc)
    zh = _SUBMISSION_ERROR_ZH.get(text)
    if zh is None:
        zh = next((message for prefix, message in _SUBMISSION_ERROR_ZH.items() if text.startswith(prefix)), text)
    return choose_text(request, text, zh)


@router.get("/courses")
def student_courses(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    courses = list_courses_for_student(db, user.id)
    return render_template(request, db, "student_courses.html", {"courses": courses})


@router.get("/llm-usage")
def student_llm_usage(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    summary = usage_summary_for_user(db, user.id)
    return render_template(request, db, "student_llm_usage.html", {"llm_usage": summary})


@router.get("/help/python-runtime")
def student_python_runtime_help(request: Request, db: Session = Depends(get_db)):
    try:
        require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    return render_template(
        request,
        db,
        "student_python_runtime_help.html",
        {
            "supported_python_version": SUPPORTED_PYTHON_VERSION,
            "supported_python_packages": SUPPORTED_PYTHON_PACKAGES,
            "default_allowed_libraries_en": default_allowed_code_libraries_text("en"),
            "default_allowed_libraries_zh": default_allowed_code_libraries_text("zh"),
            "unsupported_package_note_en": UNSUPPORTED_PACKAGE_NOTE_EN,
            "unsupported_package_note_zh": UNSUPPORTED_PACKAGE_NOTE_ZH,
        },
    )


@router.post("/courses/join")
def join_course(request: Request, join_code: str = Form(...), db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    try:
        course = join_course_by_code(db, user=user, join_code=join_code)
        push_flash(
            request,
            choose_text(request, f"You joined course {course.code}.", f"你已加入课程 {course.code}。"),
            "success",
        )
    except ValueError as exc:
        push_flash(
            request,
            choose_text(request, str(exc), "课程加入码无效。"),
            "danger",
        )
    return RedirectResponse(url="/student/courses", status_code=303)


@router.get("/courses/{course_id}")
def student_course_detail(course_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
        require_student_access(db, user.id, course_id)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    course = get_course_for_student(db, course_id, user.id)
    if course is None:
        push_flash(request, choose_text(request, "Course not found.", "未找到课程。"), "danger")
        return RedirectResponse(url="/student/courses", status_code=303)

    return render_template(request, db, "student_course_detail.html", {"course": course})


@router.get("/assignments/{assignment_id}")
def student_assignment_detail(assignment_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    assignment = get_assignment_for_student(db, assignment_id, user.id)
    if assignment is None:
        push_flash(request, choose_text(request, "Assignment not found.", "未找到作业。"), "danger")
        return RedirectResponse(url="/student/courses", status_code=303)

    return render_template(request, db, "student_assignment_detail.html", {"assignment": assignment})


@router.get("/questions/{question_id}")
def student_question_detail(question_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    question = get_question_for_student(db, question_id, user.id)
    if question is None:
        push_flash(request, choose_text(request, "Question not found.", "未找到题目。"), "danger")
        return RedirectResponse(url="/student/courses", status_code=303)

    submissions = list_submissions_for_question(db, question.id, user.id)
    return render_template(
        request,
        db,
        "student_question_detail.html",
        {"question": question, "submissions": submissions},
    )


@router.post("/questions/{question_id}/submit-notebook")
async def submit_notebook(
    question_id: int,
    request: Request,
    notebook_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    question = get_question_for_student(db, question_id, user.id)
    if question is None or question.question_type != QuestionType.NOTEBOOK:
        push_flash(
            request,
            choose_text(request, "Notebook question not found.", "未找到 Notebook 题目。"),
            "danger",
        )
        return RedirectResponse(url="/student/courses", status_code=303)

    file_bytes = await notebook_file.read()
    filename = notebook_file.filename or "submission.ipynb"
    try:
        submission = create_notebook_submission(
            db,
            user_id=user.id,
            question=question,
            original_filename=filename,
            notebook_bytes=file_bytes,
        )
        push_flash(
            request,
            choose_text(
                request,
                f"Submission #{submission.id} was received.",
                f"已收到提交 #{submission.id}。",
            ),
            "success",
        )
    except ValueError as exc:
        push_flash(request, _submission_error_message(request, exc), "danger")
    except Exception as exc:
        push_flash(
            request,
            choose_text(request, f"Notebook upload failed: {exc}", "Notebook 上传失败，请稍后重试或联系教师。"),
            "danger",
        )
    return RedirectResponse(url=f"/student/questions/{question_id}", status_code=303)


@router.post("/questions/{question_id}/submit-python")
async def submit_python_code(
    question_id: int,
    request: Request,
    code_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    return await submit_code(question_id, request, code_file, CodeLanguage.PYTHON.value, db)


@router.post("/questions/{question_id}/submit-code")
async def submit_code(
    question_id: int,
    request: Request,
    code_file: UploadFile = File(...),
    code_language: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    question = get_question_for_student(db, question_id, user.id)
    if question is None or question.question_type != QuestionType.CODE:
        push_flash(
            request,
            choose_text(request, "Code question not found.", "未找到代码题。"),
            "danger",
        )
        return RedirectResponse(url="/student/courses", status_code=303)

    file_bytes = await code_file.read()
    filename = code_file.filename or "solution.py"
    try:
        submission = create_code_submission(
            db,
            user_id=user.id,
            question=question,
            original_filename=filename,
            submission_bytes=file_bytes,
            language=code_language,
        )
        enqueue_submission_evaluation(db, submission.id)
        push_flash(
            request,
            choose_text(
                request,
                f"Submission #{submission.id} has been received and queued for evaluation.",
                f"已收到提交 #{submission.id}，系统正在排队评测。",
            ),
            "success",
        )
    except ValueError as exc:
        push_flash(request, _submission_error_message(request, exc), "danger")
    except Exception as exc:
        push_flash(
            request,
            choose_text(
                request,
                f"Failed to submit code: {exc}",
                "提交代码失败，请稍后重试或联系教师。",
            ),
            "danger",
        )
    return RedirectResponse(url=f"/student/questions/{question_id}", status_code=303)


@router.post("/questions/{question_id}/submit-text")
def submit_short_answer(
    question_id: int,
    request: Request,
    answer_text: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    question = get_question_for_student(db, question_id, user.id)
    if question is None or question.question_type != QuestionType.SHORT_ANSWER:
        push_flash(
            request,
            choose_text(request, "Short answer question not found.", "未找到简答题。"),
            "danger",
        )
        return RedirectResponse(url="/student/courses", status_code=303)

    try:
        submission = create_short_answer_submission(
            db,
            user_id=user.id,
            question=question,
            answer_text=answer_text,
        )
        if is_submission_pending_teacher_review(submission):
            push_flash(
                request,
                choose_text(
                    request,
                    f"Submission #{submission.id} was saved and is waiting for teacher review before it affects your final grade.",
                    f"提交 #{submission.id} 已保存，需等待教师确认后才会影响你的最终成绩。",
                ),
                "success",
            )
        else:
            push_flash(
                request,
                choose_text(request, f"Submission #{submission.id} was saved.", f"提交 #{submission.id} 已保存。"),
                "success",
            )
    except ValueError as exc:
        push_flash(request, _submission_error_message(request, exc), "danger")
    except Exception as exc:
        push_flash(
            request,
            choose_text(request, f"Failed to submit answer: {exc}", "提交答案失败，请稍后重试或联系教师。"),
            "danger",
        )
    return RedirectResponse(url=f"/student/questions/{question_id}", status_code=303)


@router.post("/questions/{question_id}/submit-file")
async def submit_file_question(
    question_id: int,
    request: Request,
    submission_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    question = get_question_for_student(db, question_id, user.id)
    if question is None or question.question_type not in {
        QuestionType.PDF_LLM,
        QuestionType.FORMATTED_TEXT_LLM,
        QuestionType.FILE_LLM,
    }:
        push_flash(
            request,
            choose_text(request, "File question not found.", "未找到文件题。"),
            "danger",
        )
        return RedirectResponse(url="/student/courses", status_code=303)

    file_bytes = await submission_file.read()
    filename = submission_file.filename or "submission.txt"
    try:
        submission = create_file_submission(
            db,
            user_id=user.id,
            question=question,
            original_filename=filename,
            file_bytes=file_bytes,
        )
        push_flash(
            request,
            choose_text(
                request,
                f"Submission #{submission.id} was uploaded. The LLM review flow will prepare a suggestion for teacher confirmation.",
                f"提交 #{submission.id} 已上传。系统会先生成 LLM 评阅建议，再由教师确认。",
            ),
            "success",
        )
    except ValueError as exc:
        push_flash(request, _submission_error_message(request, exc), "danger")
    except Exception as exc:
        push_flash(
            request,
            choose_text(request, f"Failed to upload file submission: {exc}", "文件提交上传失败，请稍后重试或联系教师。"),
            "danger",
        )
    return RedirectResponse(url=f"/student/questions/{question_id}", status_code=303)


@router.get("/submissions/{submission_id}")
def student_submission_detail(submission_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    submission = get_submission_for_student(db, submission_id, user.id)
    if submission is None:
        push_flash(request, choose_text(request, "Submission not found.", "未找到该提交。"), "danger")
        return RedirectResponse(url="/student/courses", status_code=303)

    latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
    feedback = sorted(submission.feedback_items, key=lambda item: item.created_at)
    pending_teacher_review = is_submission_pending_teacher_review(submission)
    return render_template(
        request,
        db,
        "student_submission_detail.html",
        {
            "submission": submission,
            "latest_result": latest_result,
            "result_view": build_student_result_view(submission),
            "feedback_items": feedback,
            "pending_teacher_review": pending_teacher_review,
            "stdout_text": read_student_safe_submission_artifact_text(latest_result, "stdout") if latest_result else "",
            "stderr_text": read_student_safe_submission_artifact_text(latest_result, "stderr") if latest_result else "",
        },
    )


@router.get("/submissions/{submission_id}/artifacts/{artifact_name}")
def student_submission_artifact(
    submission_id: int,
    artifact_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    submission = get_submission_for_student(db, submission_id, user.id)
    if submission is None:
        push_flash(request, choose_text(request, "Submission not found.", "未找到该提交。"), "danger")
        return RedirectResponse(url="/student/courses", status_code=303)

    latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
    if latest_result is None:
        push_flash(
            request,
            choose_text(request, "No evaluation result is available yet.", "当前还没有可查看的评测结果。"),
            "warning",
        )
        return RedirectResponse(url=f"/student/submissions/{submission_id}", status_code=303)

    if artifact_name not in {"stdout", "stderr"}:
        push_flash(
            request,
            choose_text(request, "This artifact is only visible to course staff.", "该产物仅课程教师与助教可见。"),
            "warning",
        )
        return RedirectResponse(url=f"/student/submissions/{submission_id}", status_code=303)

    content = read_student_safe_submission_artifact_text(latest_result, artifact_name)
    filename = f"submission-{submission_id}-{artifact_name}.txt"
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
