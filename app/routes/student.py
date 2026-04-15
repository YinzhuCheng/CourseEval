from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import push_flash
from app.constants import QuestionType
from app.db import get_db
from app.services.courses import (
    get_assignment_for_student,
    get_course_for_student,
    get_question_for_student,
    join_course_by_code,
    list_courses_for_student,
)
from app.services.permissions import RedirectRequired, require_student_access, require_user
from app.services.submissions import (
    build_student_result_view,
    create_file_submission,
    create_notebook_submission,
    create_python_code_submission,
    create_short_answer_submission,
    enqueue_file_llm_evaluation,
    enqueue_submission_evaluation,
    get_submission_for_student,
    is_submission_pending_teacher_review,
    list_submissions_for_question,
    read_student_safe_submission_artifact_text,
    resolve_submission_artifact_path,
)
from app.web import render_template


router = APIRouter(prefix="/student", tags=["student"])


@router.get("/courses")
def student_courses(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    courses = list_courses_for_student(db, user.id)
    return render_template(request, db, "student_courses.html", {"courses": courses})


@router.post("/courses/join")
def join_course(request: Request, join_code: str = Form(...), db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    try:
        course = join_course_by_code(db, user=user, join_code=join_code)
        push_flash(request, f"You joined course {course.code}.", "success")
    except ValueError as exc:
        push_flash(request, str(exc), "danger")
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
        push_flash(request, "Course not found.", "danger")
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
        push_flash(request, "Assignment not found.", "danger")
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
        push_flash(request, "Question not found.", "danger")
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
        push_flash(request, "Notebook question not found.", "danger")
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
        enqueue_submission_evaluation(db, submission.id)
        push_flash(request, f"Submission #{submission.id} created and queued.", "success")
    except ValueError as exc:
        push_flash(request, str(exc), "danger")
    except Exception as exc:
        push_flash(request, f"Failed to submit notebook: {exc}", "danger")
    return RedirectResponse(url=f"/student/questions/{question_id}", status_code=303)


@router.post("/questions/{question_id}/submit-python")
async def submit_python_code(
    question_id: int,
    request: Request,
    code_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    question = get_question_for_student(db, question_id, user.id)
    if question is None or question.question_type != QuestionType.PYTHON_CODE:
        push_flash(request, "Python code question not found.", "danger")
        return RedirectResponse(url="/student/courses", status_code=303)

    file_bytes = await code_file.read()
    filename = code_file.filename or "solution.py"
    try:
        submission = create_python_code_submission(
            db,
            user_id=user.id,
            question=question,
            original_filename=filename,
            submission_bytes=file_bytes,
        )
        enqueue_submission_evaluation(db, submission.id)
        push_flash(request, f"Submission #{submission.id} created and queued.", "success")
    except ValueError as exc:
        push_flash(request, str(exc), "danger")
    except Exception as exc:
        push_flash(request, f"Failed to submit Python code: {exc}", "danger")
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
        push_flash(request, "Short answer question not found.", "danger")
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
                f"Submission #{submission.id} saved and is waiting for teacher review before it affects your final grade.",
                "success",
            )
        else:
            push_flash(request, f"Submission #{submission.id} saved.", "success")
    except ValueError as exc:
        push_flash(request, str(exc), "danger")
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
    if question is None or question.question_type not in {QuestionType.PDF_LLM, QuestionType.FORMATTED_TEXT_LLM}:
        push_flash(request, "File question not found.", "danger")
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
        enqueue_file_llm_evaluation(db, submission.id)
        push_flash(
            request,
            f"Submission #{submission.id} uploaded. LLM review will prepare a suggestion for teacher confirmation.",
            "success",
        )
    except ValueError as exc:
        push_flash(request, str(exc), "danger")
    except Exception as exc:
        push_flash(request, f"Failed to upload file submission: {exc}", "danger")
    return RedirectResponse(url=f"/student/questions/{question_id}", status_code=303)


@router.get("/submissions/{submission_id}")
def student_submission_detail(submission_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    submission = get_submission_for_student(db, submission_id, user.id)
    if submission is None:
        push_flash(request, "Submission not found.", "danger")
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
        push_flash(request, "Submission not found.", "danger")
        return RedirectResponse(url="/student/courses", status_code=303)

    latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
    if latest_result is None:
        push_flash(request, "No evaluation result available yet.", "warning")
        return RedirectResponse(url=f"/student/submissions/{submission_id}", status_code=303)

    if artifact_name in {"stdout", "stderr"}:
        content = read_student_safe_submission_artifact_text(latest_result, artifact_name)
        filename = f"submission-{submission_id}-{artifact_name}.txt"
        return PlainTextResponse(
            content,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    try:
        artifact_path = resolve_submission_artifact_path(latest_result, artifact_name)
    except FileNotFoundError:
        push_flash(request, "Artifact not available yet.", "warning")
        return RedirectResponse(url=f"/student/submissions/{submission_id}", status_code=303)

    media_type = "text/plain"
    filename = artifact_path.name
    if artifact_name == "html":
        media_type = "text/html"
        filename = f"submission-{submission_id}.html"
    elif artifact_name == "executed_notebook":
        media_type = "application/x-ipynb+json"
        filename = f"submission-{submission_id}.ipynb"

    return FileResponse(path=artifact_path, media_type=media_type, filename=filename)
