from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import push_flash
from app.constants import CodeLanguage, CourseRole, QuestionType
from app.db import get_db
from app.i18n import choose_text
from app.runtime_support import (
    SUPPORTED_PYTHON_PACKAGES,
    SUPPORTED_PYTHON_VERSION,
    UNSUPPORTED_PACKAGE_NOTE_EN,
    UNSUPPORTED_PACKAGE_NOTE_ZH,
    default_allowed_code_libraries_text,
)
from app.services.course_materials import list_materials_for_course
from app.services.courses import (
    ensure_user_in_open_community_course,
    get_assignment_for_student,
    get_course_for_student,
    get_question_for_student,
    join_course_by_code,
    list_courses_for_student,
)
from app.services.permissions import RedirectRequired, get_course_role, require_student_access, require_user
from app.services.llm_token_usage import usage_summary_for_user
from app.services.discussion_ai import create_user_post_and_maybe_ai_reply
from app.services.discussions import (
    assignment_past_close_for_discussion,
    attach_avatar_and_role_badges,
    can_post_on_question_topic,
    display_label_for_post,
    get_or_create_question_topic,
    list_posts_for_topic,
    flat_thread_for_template,
)
from app.services.post_close_reveal import reveal_bundle_for_question
from app.services.scoring import is_submission_pending_teacher_review
from app.services.submissions import (
    build_student_result_view,
    create_file_submission,
    create_code_submission,
    create_short_answer_submission,
    enqueue_submission_evaluation,
    get_submission_for_student,
    list_submissions_for_question,
    read_student_safe_submission_artifact_text,
)
from app.web import render_template


router = APIRouter(prefix="/student", tags=["student"])


@router.get("/courses")
def student_courses(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    ensure_user_in_open_community_course(db, user)
    db.commit()
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

    materials = list_materials_for_course(db, course_id)
    return render_template(request, db, "student_course_detail.html", {"course": course, "materials": materials})


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
    topic = get_or_create_question_topic(db, question.id, question.assignment.course_id)
    db.commit()
    posts = list_posts_for_topic(db, topic.id)
    decorated = []
    for p in posts:
        label, hint = display_label_for_post(p, user, db, question.assignment.course_id)
        decorated.append({"post": p, "display_name": label, "staff_hint": hint})
    threaded = attach_avatar_and_role_badges(
        db, question.assignment.course_id, flat_thread_for_template(posts, decorated)
    )
    reveal = reveal_bundle_for_question(db, question)
    can_discuss = can_post_on_question_topic(db, question, user)
    role = get_course_role(db, question.assignment.course_id, user.id)
    disc_notice = ""
    if role == CourseRole.STUDENT and not assignment_past_close_for_discussion(question.assignment):
        disc_notice = choose_text(
            request,
            "Discussion opens after the assignment close time.",
            "讨论将在作业「关闭时间」到达后对本课程学生开放；教师与助教可随时参与。",
        )
    return render_template(
        request,
        db,
        "student_question_detail.html",
        {
            "question": question,
            "submissions": submissions,
            "topic_id": topic.id,
            "discussion_thread": threaded,
            "can_post_discussion": can_discuss,
            "reveal": reveal,
            "discussion_post_url": f"/student/questions/{question_id}/discuss",
            "discussion_notice": disc_notice,
        },
    )


@router.post("/questions/{question_id}/discuss")
def student_question_discuss(
    question_id: int,
    request: Request,
    body: str = Form(""),
    parent_post_id: str = Form(""),
    anonymous: str = Form(""),
    request_ai: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    question = get_question_for_student(db, question_id, user.id)
    if question is None:
        push_flash(request, choose_text(request, "Question not found.", "未找到题目。"), "danger")
        return RedirectResponse(url="/student/courses", status_code=303)
    if not can_post_on_question_topic(db, question, user):
        push_flash(request, choose_text(request, "Discussion is not open yet.", "讨论尚未开放。"), "danger")
        return RedirectResponse(url=f"/student/questions/{question_id}", status_code=303)
    topic = get_or_create_question_topic(db, question.id, question.assignment.course_id)
    db.commit()
    pid = int(parent_post_id) if parent_post_id.strip().isdigit() else None
    try:
        _post, ai_err = create_user_post_and_maybe_ai_reply(
            db,
            topic=topic,
            user=user,
            body=body,
            parent_post_id=pid,
            is_anonymous=(anonymous == "on" or anonymous == "true"),
            request_ai=(request_ai == "on" or request_ai == "true"),
        )
        db.commit()
    except ValueError:
        db.rollback()
        push_flash(request, choose_text(request, "Message cannot be empty.", "内容不能为空。"), "danger")
        return RedirectResponse(url=f"/student/questions/{question_id}", status_code=303)
    push_flash(request, choose_text(request, "Posted.", "已发布。"), "success")
    if ai_err:
        push_flash(request, choose_text(request, f"AI: {ai_err}", f"AI：{ai_err}"), "warning")
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
        push_flash(request, choose_text(request, str(exc), str(exc)), "danger")
    except Exception as exc:
        push_flash(
            request,
            choose_text(
                request,
                f"Failed to submit code: {exc}",
                f"提交代码失败：{exc}",
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
        push_flash(request, choose_text(request, str(exc), str(exc)), "danger")
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
    if question is None or question.question_type != QuestionType.FILE_LLM:
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
        push_flash(request, choose_text(request, str(exc), str(exc)), "danger")
    except Exception as exc:
        push_flash(
            request,
            choose_text(request, f"Failed to upload file submission: {exc}", f"文件提交上传失败：{exc}"),
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

    if artifact_name in {"stdout", "stderr"}:
        content = read_student_safe_submission_artifact_text(latest_result, artifact_name)
        filename = f"submission-{submission_id}-{artifact_name}.txt"
        return PlainTextResponse(
            content,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    push_flash(
        request,
        choose_text(request, "The requested artifact is not available.", "所请求的产物不可查看。"),
        "warning",
    )
    return RedirectResponse(url=f"/student/submissions/{submission_id}", status_code=303)
