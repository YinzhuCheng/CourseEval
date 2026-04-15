import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import nbformat
from PyPDF2 import PdfReader
from redis import Redis
from rq import Queue
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.constants import (
    EvaluationTaskStatus,
    EvaluationTaskType,
    FeedbackSource,
    JobStatus,
    MembershipStatus,
    QuestionType,
    ScoringRule,
    SubmissionLimitMode,
    SubmissionStatus,
)
from app.db import SessionLocal, utcnow
from app.services.llm import (
    generate_notebook_evaluation_with_llm,
    generate_short_answer_evaluation,
    test_llm_connectivity,
)
from app.models import (
    Assignment,
    CourseMember,
    EvaluationResult,
    EvaluationTask,
    FileQuestionConfig,
    Feedback,
    FinalGradeSnapshot,
    Job,
    JobOutput,
    Notebook,
    PythonCodeQuestionConfig,
    Question,
    RuntimeImage,
    Submission,
)


logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class RunnerResult:
    exit_code: int
    error_message: str | None = None
    summary_json: dict | None = None


def _resolve_llm_config_for_question(question: Question):
    return (
        question.llm_config
        or question.assignment.llm_config
        or question.assignment.course.default_llm_config
    )


def _clamp_score(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(value, upper))


_HIDDEN_STDOUT_MARKER = "=== Hidden Tests ==="
_HIDDEN_STDERR_MARKER = "=== Hidden Test stderr ==="


def _latest_feedback(submission: Submission, source: FeedbackSource) -> Feedback | None:
    matching_feedback = [item for item in submission.feedback_items if item.source == source]
    if not matching_feedback:
        return None
    return max(matching_feedback, key=lambda item: item.created_at)


def _parsed_summary_json(evaluation_result: EvaluationResult | None) -> dict:
    if evaluation_result is None or not evaluation_result.summary_json:
        return {}
    try:
        return json.loads(evaluation_result.summary_json)
    except json.JSONDecodeError:
        return {}


def _file_question_config(question: Question | None) -> FileQuestionConfig | None:
    return question.file_question_config if question is not None else None


def _python_code_config(question: Question | None) -> PythonCodeQuestionConfig | None:
    return question.python_code_config if question is not None else None


def _resolve_runtime_image_for_question(question: Question) -> RuntimeImage | None:
    return question.runtime_image or question.assignment.runtime_image or question.assignment.course.default_runtime_image


def _resolve_runner_image_tag(question: Question) -> tuple[str, RuntimeImage | None]:
    runtime_image = _resolve_runtime_image_for_question(question)
    if runtime_image is not None and runtime_image.image_tag:
        return runtime_image.image_tag, runtime_image
    return settings.runner_image, None


def _runner_script_path(script_name: str) -> Path:
    return settings.base_dir / "runner" / script_name


def submission_requires_teacher_confirmation(submission: Submission) -> bool:
    question = submission.question
    if submission.submission_type == QuestionType.SHORT_ANSWER:
        config = question.short_answer_config if question is not None else None
        return config is None or config.teacher_confirmation_required
    if submission.submission_type in {QuestionType.PDF_LLM, QuestionType.FORMATTED_TEXT_LLM}:
        config = _file_question_config(question)
        return config is None or config.teacher_confirmation_required
    return False


def submission_has_teacher_feedback(submission: Submission) -> bool:
    return _latest_feedback(submission, FeedbackSource.TEACHER) is not None


def is_submission_pending_teacher_review(submission: Submission) -> bool:
    return submission_requires_teacher_confirmation(submission) and not submission_has_teacher_feedback(submission)


def resolve_submission_score(submission: Submission) -> tuple[Decimal | None, FeedbackSource | None]:
    teacher_feedback = _latest_feedback(submission, FeedbackSource.TEACHER)
    if teacher_feedback is not None:
        return (
            Decimal(str(teacher_feedback.score_suggestion)) if teacher_feedback.score_suggestion is not None else None,
            FeedbackSource.TEACHER,
        )

    latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
    if latest_result is not None and latest_result.final_score is not None:
        return Decimal(str(latest_result.final_score)), FeedbackSource.AUTO

    if submission_requires_teacher_confirmation(submission):
        return None, None

    llm_feedback = _latest_feedback(submission, FeedbackSource.LLM)
    if llm_feedback is not None and llm_feedback.score_suggestion is not None:
        return Decimal(str(llm_feedback.score_suggestion)), FeedbackSource.LLM

    auto_feedback = _latest_feedback(submission, FeedbackSource.AUTO)
    if auto_feedback is not None and auto_feedback.score_suggestion is not None:
        return Decimal(str(auto_feedback.score_suggestion)), FeedbackSource.AUTO

    return None, None


def build_student_result_view(submission: Submission) -> dict:
    latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
    summary = _parsed_summary_json(latest_result)
    score_value, score_source = resolve_submission_score(submission)
    hidden_message = summary.get("hidden_message")
    hidden_checks_applied = bool(
        summary.get("hidden_weight")
        or (
            hidden_message is not None
            and str(hidden_message).strip()
            and str(hidden_message).strip() != "No tests configured."
        )
    )
    return {
        "score": score_value,
        "score_source": score_source,
        "run_success": latest_result.run_success if latest_result is not None else None,
        "visible_score": latest_result.visible_score if latest_result is not None else None,
        "auto_score": latest_result.auto_score if latest_result is not None else None,
        "message": summary.get("message"),
        "visible_message": summary.get("visible_message"),
        "failure_type": summary.get("failure_type"),
        "hidden_checks_applied": hidden_checks_applied,
    }


def _strip_hidden_output_sections(content: str, markers: set[str]) -> tuple[str, bool]:
    if not content:
        return "", False

    sanitized_lines: list[str] = []
    skipping_hidden_block = False
    content_changed = False
    for line in content.splitlines():
        normalized = line.strip()
        if normalized in markers:
            skipping_hidden_block = True
            content_changed = True
            continue
        if skipping_hidden_block and normalized.startswith("===") and normalized.endswith("==="):
            skipping_hidden_block = False
        if skipping_hidden_block:
            content_changed = True
            continue
        sanitized_lines.append(line)

    sanitized = "\n".join(sanitized_lines).strip()
    if content.endswith("\n") and sanitized:
        sanitized = f"{sanitized}\n"
    return sanitized, content_changed


def read_student_safe_submission_artifact_text(
    evaluation_result: EvaluationResult | None,
    artifact_name: str,
    max_chars: int = 200000,
) -> str:
    content = read_submission_artifact_text(evaluation_result, artifact_name, max_chars=max_chars)
    if artifact_name == "stdout":
        sanitized, changed = _strip_hidden_output_sections(content, {_HIDDEN_STDOUT_MARKER})
    elif artifact_name == "stderr":
        sanitized, changed = _strip_hidden_output_sections(content, {_HIDDEN_STDERR_MARKER})
    else:
        return content

    if changed and not sanitized:
        return "Hidden test details are not shown in the student view.\n"
    return sanitized


def _recompute_notebook_final_score(submission: Submission, latest_result: EvaluationResult) -> Decimal:
    auto_score = Decimal(str(latest_result.auto_score or 0))
    notebook_config = submission.question.notebook_config if submission.question else None
    llm_weight = Decimal(str(notebook_config.llm_score_weight if notebook_config else 0))
    llm_score_candidates = [
        Decimal(str(item.score_suggestion or 0))
        for item in submission.feedback_items
        if item.source == FeedbackSource.LLM and item.score_suggestion is not None
    ]
    llm_score = llm_score_candidates[-1] if llm_score_candidates else Decimal("0")
    llm_score = _clamp_score(llm_score, Decimal("0"), llm_weight)
    max_score = Decimal(str(submission.question.max_score if submission.question else 100))
    return _clamp_score(auto_score + llm_score, Decimal("0"), max_score)


def redis_connection() -> Redis:
    return Redis.from_url(settings.redis_url)


def get_queue() -> Queue:
    return Queue(settings.rq_queue_name, connection=redis_connection())


def relative_to_data(path: Path) -> str:
    return path.relative_to(settings.data_dir).as_posix()


def absolute_data_path(relative_path: str) -> Path:
    return (settings.data_dir / relative_path).resolve()


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_writable_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o777)


def write_text(path: Path, content: str, append: bool = False) -> None:
    ensure_parent_dir(path)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as file_handle:
        file_handle.write(content)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _store_uploaded_file(*, user_id: int, question_id: int, original_filename: str, file_bytes: bytes) -> tuple[str, str]:
    suffix = Path(original_filename).suffix.lower() or ".bin"
    upload_dir = settings.uploads_dir / f"user-{user_id}" / f"question-{question_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = upload_dir / f"{uuid4().hex}{suffix}"
    stored_path.write_bytes(file_bytes)
    return relative_to_data(stored_path), stored_path.name


def _ensure_text_file_extension(filename: str, allowed_extensions: set[str]) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in allowed_extensions:
        raise ValueError(f"File type {extension or '(none)'} is not allowed for this question.")
    return extension


def _extract_pdf_text(file_path: Path) -> str:
    reader = PdfReader(file_path)
    extracted_pages = [page.extract_text() or "" for page in reader.pages]
    extracted_text = "\n\n".join(page_text.strip() for page_text in extracted_pages if page_text.strip()).strip()
    if not extracted_text:
        raise ValueError("The uploaded PDF does not contain extractable text. Phase 1 supports text-based PDFs only.")
    return extracted_text


def _render_notebook_as_text(file_path: Path, *, require_outputs: bool) -> str:
    notebook = nbformat.read(file_path, as_version=4)
    has_outputs = False
    sections: list[str] = []
    for index, cell in enumerate(notebook.cells, start=1):
        cell_type = cell.get("cell_type", "unknown")
        sections.append(f"Cell {index} [{cell_type}]")
        source = (cell.get("source") or "").strip()
        if source:
            sections.append(source)
        outputs = cell.get("outputs") or []
        if outputs:
            has_outputs = True
            rendered_outputs: list[str] = []
            for output in outputs:
                text = ""
                if output.get("output_type") == "stream":
                    text = output.get("text", "")
                elif "text" in output:
                    text = output.get("text", "")
                elif "data" in output and isinstance(output["data"], dict):
                    text = output["data"].get("text/plain", "")
                if text:
                    rendered_outputs.append(str(text).strip())
            if rendered_outputs:
                sections.append("Outputs:")
                sections.append("\n".join(item for item in rendered_outputs if item))
        sections.append("")
    if require_outputs and not has_outputs:
        raise ValueError("Notebook submissions for this question must include executed outputs before upload.")
    rendered = "\n".join(section for section in sections if section is not None).strip()
    if not rendered:
        raise ValueError("The uploaded notebook is empty.")
    return rendered


def _extract_formatted_text(file_path: Path, extension: str, *, require_ipynb_output: bool) -> str:
    if extension in {".txt", ".tex", ".py"}:
        return file_path.read_text(encoding="utf-8", errors="replace").strip()
    if extension == ".ipynb":
        return _render_notebook_as_text(file_path, require_outputs=require_ipynb_output)
    raise ValueError(f"Unsupported formatted-text file type: {extension}.")


def _parse_test_cases_json(raw_json: str) -> list[dict]:
    try:
        payload = json.loads(raw_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Stored Python code test cases are not valid JSON.") from exc
    if not isinstance(payload, list):
        raise ValueError("Stored Python code test cases must be a JSON list.")
    normalized: list[dict] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError("Each Python code test case must be a JSON object.")
        normalized.append(
            {
                "name": item.get("name") or f"Test {index}",
                "input": item.get("input", ""),
                "expected_output": item.get("expected_output", item.get("output", "")),
                "points": float(item.get("points", 20)),
            }
        )
    return normalized


def create_legacy_job_with_upload(
    db: Session,
    *,
    user_id: int,
    original_filename: str,
    notebook_bytes: bytes,
) -> Job:
    upload_dir = settings.uploads_dir / f"user-{user_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)

    stored_path = upload_dir / f"{uuid4().hex}.ipynb"
    stored_path.write_bytes(notebook_bytes)

    notebook = Notebook(
        user_id=user_id,
        original_filename=original_filename,
        stored_path=relative_to_data(stored_path),
    )
    db.add(notebook)
    db.flush()

    job = Job(user_id=user_id, notebook_id=notebook.id, status=JobStatus.QUEUED)
    db.add(job)
    db.flush()

    output_dir = settings.outputs_dir / f"job-{job.id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    output = JobOutput(
        job_id=job.id,
        executed_notebook_path=relative_to_data(output_dir / "executed.ipynb"),
        html_path=relative_to_data(output_dir / "executed.html"),
        stdout_path=relative_to_data(output_dir / "stdout.txt"),
        stderr_path=relative_to_data(output_dir / "stderr.txt"),
    )
    db.add(output)
    db.commit()
    db.refresh(job)
    return job


def enqueue_legacy_job(job_id: int) -> str:
    rq_job = get_queue().enqueue(
        process_legacy_job,
        job_id,
        job_timeout=settings.execution_timeout_seconds + 90,
        result_ttl=86400,
        failure_ttl=86400,
    )
    return rq_job.id


def list_jobs_for_user(db: Session, user_id: int) -> list[Job]:
    statement = (
        select(Job)
        .options(joinedload(Job.notebook), joinedload(Job.output))
        .where(Job.user_id == user_id)
        .order_by(Job.created_at.desc())
    )
    return list(db.scalars(statement).unique())


def get_job_for_user(db: Session, job_id: int, user_id: int) -> Job | None:
    statement = (
        select(Job)
        .options(joinedload(Job.notebook), joinedload(Job.output))
        .where(Job.id == job_id, Job.user_id == user_id)
    )
    return db.scalar(statement)


def get_artifact_path_from_job(job: Job, artifact_name: str) -> Path:
    if job.output is None:
        raise FileNotFoundError("Job output metadata is missing.")

    mapping = {
        "executed_notebook": job.output.executed_notebook_path,
        "html": job.output.html_path,
        "stdout": job.output.stdout_path,
        "stderr": job.output.stderr_path,
    }
    relative_path = mapping.get(artifact_name)
    if not relative_path:
        raise FileNotFoundError("Unknown artifact.")
    artifact_path = absolute_data_path(relative_path)
    if not artifact_path.exists():
        raise FileNotFoundError("Artifact file does not exist.")
    return artifact_path


def read_text_artifact_from_job(job: Job, artifact_name: str, max_chars: int = 200000) -> str:
    try:
        artifact_path = get_artifact_path_from_job(job, artifact_name)
    except FileNotFoundError:
        return ""

    content = artifact_path.read_text(encoding="utf-8", errors="replace")
    if len(content) > max_chars:
        return f"{content[:max_chars]}\n\n... output truncated in web view ..."
    return content


def list_student_courses(db: Session, user_id: int) -> list:
    statement = (
        select(CourseMember)
        .options(joinedload(CourseMember.course))
        .where(
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
        .order_by(CourseMember.joined_at.desc())
    )
    return list(db.scalars(statement).unique())


def list_course_assignments_for_student(db: Session, course_id: int) -> list[Assignment]:
    statement = (
        select(Assignment)
        .where(Assignment.course_id == course_id)
        .order_by(Assignment.created_at.desc())
    )
    return list(db.scalars(statement))


def get_question_for_student(db: Session, question_id: int, user_id: int) -> Question | None:
    statement = (
        select(Question)
        .options(
            joinedload(Question.assignment).joinedload(Assignment.course),
            joinedload(Question.notebook_config),
            joinedload(Question.python_code_config),
            joinedload(Question.file_question_config),
            joinedload(Question.short_answer_config),
        )
        .join(Assignment, Question.assignment_id == Assignment.id)
        .join(CourseMember, and_(CourseMember.course_id == Assignment.course_id, CourseMember.user_id == user_id))
        .where(
            Question.id == question_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    return db.scalar(statement)


def list_submissions_for_question(db: Session, question_id: int, user_id: int) -> list[Submission]:
    statement = (
        select(Submission)
        .options(
            joinedload(Submission.evaluation_results),
            joinedload(Submission.feedback_items),
        )
        .where(Submission.question_id == question_id, Submission.user_id == user_id)
        .order_by(Submission.submitted_at.desc())
    )
    return list(db.scalars(statement).unique())


def get_submission_for_student(db: Session, submission_id: int, user_id: int) -> Submission | None:
    statement = (
        select(Submission)
        .options(
            joinedload(Submission.question).joinedload(Question.assignment).joinedload(Assignment.course),
            joinedload(Submission.question).joinedload(Question.notebook_config),
            joinedload(Submission.question).joinedload(Question.python_code_config),
            joinedload(Submission.question).joinedload(Question.file_question_config),
            joinedload(Submission.question).joinedload(Question.short_answer_config),
            joinedload(Submission.evaluation_tasks),
            joinedload(Submission.evaluation_results),
            joinedload(Submission.feedback_items),
            joinedload(Submission.notebook),
        )
        .where(Submission.id == submission_id, Submission.user_id == user_id)
    )
    return db.scalar(statement)


def get_submission_for_teacher(db: Session, submission_id: int, teacher_id: int) -> Submission | None:
    statement = (
        select(Submission)
        .options(
            joinedload(Submission.user),
            joinedload(Submission.question).joinedload(Question.assignment).joinedload(Assignment.course),
            joinedload(Submission.question).joinedload(Question.notebook_config),
            joinedload(Submission.question).joinedload(Question.python_code_config),
            joinedload(Submission.question).joinedload(Question.file_question_config),
            joinedload(Submission.question).joinedload(Question.short_answer_config),
            joinedload(Submission.evaluation_tasks),
            joinedload(Submission.evaluation_results),
            joinedload(Submission.feedback_items),
            joinedload(Submission.notebook),
        )
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .join(
            CourseMember,
            and_(
                CourseMember.course_id == Assignment.course_id,
                CourseMember.user_id == teacher_id,
                CourseMember.role.in_(["teacher", "ta"]),
            ),
        )
        .where(Submission.id == submission_id, CourseMember.status == MembershipStatus.ACTIVE)
    )
    return db.scalar(statement)


def list_submissions_for_student_question(db: Session, user_id: int, question_id: int) -> list[Submission]:
    return list_submissions_for_question(db, question_id, user_id)


def read_submission_artifact_text(
    evaluation_result: EvaluationResult | None,
    artifact_name: str,
    max_chars: int = 200000,
) -> str:
    if evaluation_result is None:
        return ""
    mapping = {
        "stdout": evaluation_result.stdout_path,
        "stderr": evaluation_result.stderr_path,
        "summary": evaluation_result.log_path,
    }
    relative_path = mapping.get(artifact_name)
    if not relative_path:
        return ""
    artifact_path = absolute_data_path(relative_path)
    if not artifact_path.exists():
        return ""
    content = artifact_path.read_text(encoding="utf-8", errors="replace")
    if len(content) > max_chars:
        return f"{content[:max_chars]}\n\n... output truncated in web view ..."
    return content


def resolve_submission_artifact_path(evaluation_result: EvaluationResult, artifact_name: str) -> Path:
    mapping = {
        "executed_notebook": evaluation_result.executed_notebook_path,
        "html": evaluation_result.rendered_html_path,
        "stdout": evaluation_result.stdout_path,
        "stderr": evaluation_result.stderr_path,
        "summary": evaluation_result.log_path,
    }
    relative_path = mapping.get(artifact_name)
    if not relative_path:
        raise FileNotFoundError("Unknown artifact.")
    artifact_path = absolute_data_path(relative_path)
    if not artifact_path.exists():
        raise FileNotFoundError("Artifact file does not exist.")
    return artifact_path


def refresh_final_grade_snapshot(db: Session, question_id: int, user_id: int) -> None:
    submission = db.scalar(
        select(Submission)
        .options(joinedload(Submission.question), joinedload(Submission.evaluation_results), joinedload(Submission.feedback_items))
        .where(Submission.question_id == question_id, Submission.user_id == user_id)
        .order_by(Submission.submitted_at.desc())
    )
    if submission is not None:
        update_final_grade_snapshot(db, submission)


def _question_scoring_rule(question: Question) -> ScoringRule:
    return question.scoring_rule_override or question.assignment.default_scoring_rule


def _is_late(question: Question) -> bool:
    now = _now()
    due_at = question.assignment.due_at
    if due_at is None:
        return False
    return now > due_at


def _submission_window_open(question: Question) -> tuple[bool, str | None]:
    now = _now()
    assignment = question.assignment
    if assignment.open_at and now < assignment.open_at:
        return False, "Submission window has not opened yet."
    if assignment.close_at and now > assignment.close_at:
        return False, "Submission window is closed."
    if assignment.due_at and now > assignment.due_at and not assignment.allow_late:
        return False, "Late submissions are not allowed for this assignment."
    return True, None


def _count_attempts(db: Session, user_id: int, question_id: int, start_of_day: datetime | None = None) -> int:
    filters = [
        Submission.user_id == user_id,
        Submission.question_id == question_id,
        Submission.counts_toward_limit.is_(True),
    ]
    if start_of_day is not None:
        filters.append(Submission.submitted_at >= start_of_day)
    statement = select(func.count(Submission.id)).where(*filters)
    return int(db.scalar(statement) or 0)


def _check_submission_limit(db: Session, question: Question, user_id: int) -> tuple[bool, str | None]:
    assignment = question.assignment
    mode = assignment.submission_limit_mode
    limit = assignment.submission_limit_value
    if mode == SubmissionLimitMode.UNLIMITED or not limit:
        return True, None

    if mode == SubmissionLimitMode.TOTAL:
        used = _count_attempts(db, user_id, question.id)
        if used >= limit:
            return False, f"You have already used the maximum total submissions for this question ({limit})."
        return True, None

    if mode == SubmissionLimitMode.DAILY:
        now = _now()
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        used = _count_attempts(db, user_id, question.id, start_of_day=start_of_day)
        if used >= limit:
            return False, f"You have already used the daily submission limit for this question ({limit})."
        return True, None

    return True, None


def create_notebook_submission(
    db: Session,
    *,
    user_id: int,
    question: Question,
    original_filename: str,
    notebook_bytes: bytes,
) -> Submission:
    allowed, message = _submission_window_open(question)
    if not allowed:
        raise ValueError(message or "Submission window is closed.")

    allowed, message = _check_submission_limit(db, question, user_id)
    if not allowed:
        raise ValueError(message or "Submission limit reached.")

    upload_dir = settings.uploads_dir / f"user-{user_id}" / f"question-{question.id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = upload_dir / f"{uuid4().hex}.ipynb"
    stored_path.write_bytes(notebook_bytes)

    notebook = Notebook(
        user_id=user_id,
        original_filename=original_filename,
        stored_path=relative_to_data(stored_path),
    )
    db.add(notebook)
    db.flush()

    submission = Submission(
        course_id=question.assignment.course_id,
        assignment_id=question.assignment_id,
        question_id=question.id,
        user_id=user_id,
        submission_type=QuestionType.NOTEBOOK,
        status=SubmissionStatus.SUBMITTED,
        original_filename=original_filename,
        notebook_id=notebook.id,
        submitted_at=utcnow(),
        is_late=_is_late(question),
        counts_toward_limit=False,
        is_effective_submission=False,
    )
    db.add(submission)
    db.flush()

    task = EvaluationTask(
        submission_id=submission.id,
        task_type=EvaluationTaskType.NOTEBOOK_EVALUATION,
        backend_type="rq",
        status=EvaluationTaskStatus.QUEUED,
    )
    db.add(task)
    db.commit()
    db.refresh(submission)
    return submission


def create_short_answer_submission(
    db: Session,
    *,
    user_id: int,
    question: Question,
    answer_text: str,
) -> Submission:
    allowed, message = _submission_window_open(question)
    if not allowed:
        raise ValueError(message or "Submission window is closed.")

    allowed, message = _check_submission_limit(db, question, user_id)
    if not allowed:
        raise ValueError(message or "Submission limit reached.")

    config = question.short_answer_config
    teacher_confirmation_required = True if config is None else config.teacher_confirmation_required
    cleaned = answer_text.strip()
    if not cleaned:
        raise ValueError("Answer cannot be empty.")
    if config and config.min_length and len(cleaned) < config.min_length:
        raise ValueError(f"Answer must be at least {config.min_length} characters long.")
    if config and config.max_length and len(cleaned) > config.max_length:
        raise ValueError(f"Answer must be at most {config.max_length} characters long.")

    submission = Submission(
        course_id=question.assignment.course_id,
        assignment_id=question.assignment_id,
        question_id=question.id,
        user_id=user_id,
        submission_type=QuestionType.SHORT_ANSWER,
        status=SubmissionStatus.SUBMITTED if teacher_confirmation_required else SubmissionStatus.COMPLETED,
        answer_text=cleaned,
        submitted_at=utcnow(),
        completed_at=None if teacher_confirmation_required else utcnow(),
        is_late=_is_late(question),
        counts_toward_limit=True,
        is_effective_submission=not teacher_confirmation_required,
    )
    db.add(submission)
    db.flush()
    if config and config.llm_suggestion_enabled:
        db.add(
            EvaluationTask(
                submission_id=submission.id,
                task_type=EvaluationTaskType.SHORT_ANSWER_LLM,
                backend_type="rq",
                status=EvaluationTaskStatus.QUEUED,
            )
        )
    db.commit()
    db.refresh(submission)
    if not teacher_confirmation_required:
        update_final_grade_snapshot(db, submission)
    if config and config.llm_suggestion_enabled:
        enqueue_short_answer_llm(db, submission.id)
    return submission


def create_python_code_submission(
    db: Session,
    *,
    user_id: int,
    question: Question,
    original_filename: str,
    submission_bytes: bytes,
) -> Submission:
    allowed, message = _submission_window_open(question)
    if not allowed:
        raise ValueError(message or "Submission window is closed.")

    allowed, message = _check_submission_limit(db, question, user_id)
    if not allowed:
        raise ValueError(message or "Submission limit reached.")

    config = _python_code_config(question)
    if config is None:
        raise ValueError("Python code configuration is missing for this question.")
    _ensure_text_file_extension(original_filename, {".py"})
    stored_relative_path, _ = _store_uploaded_file(
        user_id=user_id,
        question_id=question.id,
        original_filename=original_filename,
        file_bytes=submission_bytes,
    )
    source_text = absolute_data_path(stored_relative_path).read_text(encoding="utf-8", errors="replace").strip()
    if not source_text:
        raise ValueError("Uploaded Python file is empty.")

    submission = Submission(
        course_id=question.assignment.course_id,
        assignment_id=question.assignment_id,
        question_id=question.id,
        user_id=user_id,
        submission_type=QuestionType.PYTHON_CODE,
        status=SubmissionStatus.SUBMITTED,
        original_filename=original_filename,
        stored_file_path=stored_relative_path,
        answer_text=source_text,
        submitted_at=utcnow(),
        is_late=_is_late(question),
        counts_toward_limit=False,
        is_effective_submission=False,
    )
    db.add(submission)
    db.flush()

    db.add(
        EvaluationTask(
            submission_id=submission.id,
            task_type=EvaluationTaskType.PYTHON_CODE_EVALUATION,
            backend_type="rq",
            status=EvaluationTaskStatus.QUEUED,
        )
    )
    db.commit()
    db.refresh(submission)
    return submission


def create_file_submission(
    db: Session,
    *,
    user_id: int,
    question: Question,
    original_filename: str,
    file_bytes: bytes,
) -> Submission:
    allowed, message = _submission_window_open(question)
    if not allowed:
        raise ValueError(message or "Submission window is closed.")

    allowed, message = _check_submission_limit(db, question, user_id)
    if not allowed:
        raise ValueError(message or "Submission limit reached.")

    config = _file_question_config(question)
    if config is None:
        raise ValueError("File question configuration is missing for this question.")

    allowed_extensions = {item.strip().lower() for item in config.accepted_extensions.split(",") if item.strip()}
    extension = _ensure_text_file_extension(original_filename, allowed_extensions)
    stored_relative_path, _ = _store_uploaded_file(
        user_id=user_id,
        question_id=question.id,
        original_filename=original_filename,
        file_bytes=file_bytes,
    )
    stored_path = absolute_data_path(stored_relative_path)
    if question.question_type == QuestionType.PDF_LLM:
        extracted_text = _extract_pdf_text(stored_path)
    else:
        extracted_text = _extract_formatted_text(
            stored_path,
            extension,
            require_ipynb_output=config.notebook_outputs_required,
        )
    if not extracted_text:
        raise ValueError("The uploaded file does not contain any extractable content.")

    llm_enabled = config.llm_suggestion_enabled
    teacher_confirmation_required = config.teacher_confirmation_required
    submission = Submission(
        course_id=question.assignment.course_id,
        assignment_id=question.assignment_id,
        question_id=question.id,
        user_id=user_id,
        submission_type=question.question_type,
        status=SubmissionStatus.SUBMITTED if teacher_confirmation_required or llm_enabled else SubmissionStatus.COMPLETED,
        original_filename=original_filename,
        stored_file_path=stored_relative_path,
        answer_text=extracted_text,
        submitted_at=utcnow(),
        completed_at=utcnow() if not teacher_confirmation_required and not llm_enabled else None,
        is_late=_is_late(question),
        counts_toward_limit=True,
        is_effective_submission=not teacher_confirmation_required and not llm_enabled,
    )
    db.add(submission)
    db.flush()
    if llm_enabled:
        db.add(
            EvaluationTask(
                submission_id=submission.id,
                task_type=EvaluationTaskType.FILE_LLM_EVALUATION,
                backend_type="rq",
                status=EvaluationTaskStatus.QUEUED,
            )
        )
    db.commit()
    db.refresh(submission)
    if llm_enabled:
        enqueue_file_llm_evaluation(db, submission.id)
    elif submission.is_effective_submission:
        update_final_grade_snapshot(db, submission)
    return submission


def enqueue_submission_evaluation(db: Session, submission_id: int) -> str:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise ValueError("Submission not found.")

    statement = (
        select(EvaluationTask)
        .where(EvaluationTask.submission_id == submission_id)
        .order_by(EvaluationTask.created_at.desc())
    )
    task = db.scalars(statement).first()
    if task is None:
        raise ValueError("Evaluation task not found.")

    if task.task_type == EvaluationTaskType.PYTHON_CODE_EVALUATION:
        target_func = process_python_code_evaluation
        timeout_seconds = settings.execution_timeout_seconds + 60
    else:
        target_func = process_submission_evaluation
        timeout_seconds = settings.execution_timeout_seconds + 120

    rq_job = get_queue().enqueue(
        target_func,
        submission_id,
        task.id,
        job_timeout=timeout_seconds,
        result_ttl=86400,
        failure_ttl=86400,
    )
    submission.status = SubmissionStatus.QUEUED
    submission.queued_at = utcnow()
    task.backend_job_id = rq_job.id
    task.status = EvaluationTaskStatus.QUEUED
    db.commit()
    return rq_job.id


def enqueue_short_answer_llm(db: Session, submission_id: int) -> str:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise ValueError("Submission not found.")

    task = db.scalar(
        select(EvaluationTask)
        .where(
            EvaluationTask.submission_id == submission_id,
            EvaluationTask.task_type == EvaluationTaskType.SHORT_ANSWER_LLM,
        )
        .order_by(EvaluationTask.created_at.desc())
    )
    if task is None:
        raise ValueError("Short-answer LLM task not found.")

    rq_job = get_queue().enqueue(
        process_short_answer_llm_evaluation,
        submission_id,
        task.id,
        job_timeout=120,
        result_ttl=86400,
        failure_ttl=86400,
    )
    task.backend_job_id = rq_job.id
    task.status = EvaluationTaskStatus.QUEUED
    db.commit()
    return rq_job.id


def enqueue_file_llm_evaluation(db: Session, submission_id: int) -> str:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise ValueError("Submission not found.")

    task = db.scalar(
        select(EvaluationTask)
        .where(
            EvaluationTask.submission_id == submission_id,
            EvaluationTask.task_type == EvaluationTaskType.FILE_LLM_EVALUATION,
        )
        .order_by(EvaluationTask.created_at.desc())
    )
    if task is None:
        raise ValueError("File LLM task not found.")

    rq_job = get_queue().enqueue(
        process_file_llm_evaluation,
        submission_id,
        task.id,
        job_timeout=120,
        result_ttl=86400,
        failure_ttl=86400,
    )
    task.backend_job_id = rq_job.id
    task.status = EvaluationTaskStatus.QUEUED
    db.commit()
    return rq_job.id


def enqueue_notebook_llm_feedback(db: Session, submission_id: int) -> str:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise ValueError("Submission not found.")

    task = db.scalar(
        select(EvaluationTask)
        .where(
            EvaluationTask.submission_id == submission_id,
            EvaluationTask.task_type == EvaluationTaskType.NOTEBOOK_LLM_FEEDBACK,
        )
        .order_by(EvaluationTask.created_at.desc())
    )
    if task is None:
        task = EvaluationTask(
            submission_id=submission_id,
            task_type=EvaluationTaskType.NOTEBOOK_LLM_FEEDBACK,
            backend_type="rq",
            status=EvaluationTaskStatus.QUEUED,
        )
        db.add(task)
        db.flush()

    rq_job = get_queue().enqueue(
        process_notebook_llm_feedback,
        submission_id,
        task.id,
        job_timeout=120,
        result_ttl=86400,
        failure_ttl=86400,
    )
    task.backend_job_id = rq_job.id
    task.status = EvaluationTaskStatus.QUEUED
    db.commit()
    return rq_job.id


def cleanup_stale_running_items() -> int:
    with SessionLocal() as db:
        running_jobs = list(db.scalars(select(Job).where(Job.status == JobStatus.RUNNING)).all())
        running_submissions = list(
            db.scalars(select(Submission).where(Submission.status == SubmissionStatus.RUNNING)).all()
        )
        running_tasks = list(
            db.scalars(select(EvaluationTask).where(EvaluationTask.status == EvaluationTaskStatus.RUNNING)).all()
        )

        finished_at = utcnow()
        for job in running_jobs:
            job.status = JobStatus.FAILED
            job.finished_at = finished_at
            job.exit_code = -1
            job.error_message = "Worker restarted before this notebook job completed."

        for submission in running_submissions:
            submission.status = SubmissionStatus.FAILED_SYSTEM
            submission.completed_at = finished_at
            submission.counts_toward_limit = False
            submission.is_effective_submission = False
            submission.failure_reason_code = "worker_restart"

        for task in running_tasks:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = finished_at
            task.error_message = "Worker restarted before this evaluation task completed."

        db.commit()
        return len(running_jobs) + len(running_submissions)


def process_legacy_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        statement = (
            select(Job)
            .options(joinedload(Job.notebook), joinedload(Job.output))
            .where(Job.id == job_id)
        )
        job = db.scalar(statement)
        if job is None or job.output is None or job.notebook is None:
            logger.error("Legacy job %s could not be loaded for execution.", job_id)
            return

        job.status = JobStatus.RUNNING
        job.started_at = utcnow()
        job.finished_at = None
        job.exit_code = None
        job.error_message = None
        db.commit()

        result = run_job_in_docker(
            input_relative_path=job.notebook.stored_path,
            output_dir_relative_path=Path(job.output.executed_notebook_path).parent.as_posix(),
            runner_image=settings.runner_image,
            timeout_seconds=settings.execution_timeout_seconds,
            memory_limit=settings.runner_memory_limit,
            cpus=settings.runner_cpus,
            network_disabled=settings.docker_network_disabled,
        )

        job.exit_code = result.exit_code
        job.finished_at = utcnow()
        if result.exit_code == 0:
            job.status = JobStatus.SUCCESS
            job.error_message = None
        else:
            job.status = JobStatus.FAILED
            job.error_message = result.error_message or "Notebook execution failed."
        db.commit()
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error while processing legacy job %s", job_id)
        failed_job = db.get(Job, job_id)
        if failed_job is not None:
            failed_job.status = JobStatus.FAILED
            failed_job.finished_at = utcnow()
            failed_job.exit_code = -1
            failed_job.error_message = str(exc)
            db.commit()
    finally:
        db.close()


def process_submission_evaluation(submission_id: int, task_id: int) -> None:
    db = SessionLocal()
    try:
        statement = (
            select(Submission)
            .options(
                joinedload(Submission.notebook),
                joinedload(Submission.question).joinedload(Question.notebook_config),
                joinedload(Submission.assignment),
                joinedload(Submission.evaluation_results),
                joinedload(Submission.evaluation_tasks),
            )
            .where(Submission.id == submission_id)
        )
        submission = db.scalar(statement)
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None or submission.notebook is None:
            logger.error("Submission %s or task %s could not be loaded for evaluation.", submission_id, task_id)
            return

        question = submission.question
        notebook_config = question.notebook_config
        runtime_image_tag, runtime_image = _resolve_runner_image_tag(question)
        timeout_seconds = notebook_config.time_limit_seconds if notebook_config else settings.execution_timeout_seconds
        memory_limit = (
            f"{notebook_config.memory_limit_mb}m" if notebook_config else settings.runner_memory_limit
        )
        cpus = notebook_config.cpu_limit if notebook_config else settings.runner_cpus
        network_disabled = not (notebook_config.allow_network if notebook_config else False)

        submission.status = SubmissionStatus.RUNNING
        submission.started_at = utcnow()
        task.status = EvaluationTaskStatus.RUNNING
        task.runtime_image_id = runtime_image.id if runtime_image is not None else None
        task.started_at = utcnow()
        task.error_message = None
        db.commit()

        output_dir = settings.outputs_dir / "submissions" / f"submission-{submission.id}"
        ensure_writable_directory(output_dir)

        result = run_job_in_docker(
            input_relative_path=submission.notebook.stored_path,
            output_dir_relative_path=relative_to_data(output_dir),
            runner_image=runtime_image_tag,
            timeout_seconds=timeout_seconds,
            memory_limit=memory_limit,
            cpus=cpus,
            network_disabled=network_disabled,
            visible_tests_source=notebook_config.visible_tests_source if notebook_config else "",
            hidden_tests_source=notebook_config.hidden_tests_source if notebook_config else "",
            execution_weight=str(notebook_config.execution_weight if notebook_config else Decimal("0")),
            visible_weight=str(notebook_config.visible_weight if notebook_config else Decimal("100")),
            hidden_weight=str(notebook_config.hidden_weight if notebook_config else Decimal("0")),
        )

        evaluation_result = EvaluationResult(
            submission_id=submission.id,
            evaluation_task_id=task.id,
            run_success=result.exit_code == 0,
            visible_score=Decimal(str(result.summary_json.get("visible_score", 0))) if result.summary_json else Decimal("0"),
            hidden_score=Decimal(str(result.summary_json.get("hidden_score", 0))) if result.summary_json else Decimal("0"),
            auto_score=Decimal(str(result.summary_json.get("auto_score", 0))) if result.summary_json else Decimal("0"),
            final_score=Decimal(str(result.summary_json.get("auto_score", 0))) if result.summary_json else Decimal("0"),
            log_path=relative_to_data(output_dir / "summary.json"),
            stdout_path=relative_to_data(output_dir / "stdout.txt"),
            stderr_path=relative_to_data(output_dir / "stderr.txt"),
            rendered_html_path=relative_to_data(output_dir / "executed.html"),
            executed_notebook_path=relative_to_data(output_dir / "executed.ipynb"),
            summary_json=json.dumps(result.summary_json or {}, ensure_ascii=True, indent=2),
        )
        db.add(evaluation_result)

        task.finished_at = utcnow()
        if result.exit_code == 0:
            task.status = EvaluationTaskStatus.SUCCEEDED
            submission.status = SubmissionStatus.COMPLETED
            submission.completed_at = utcnow()
            submission.counts_toward_limit = True
            submission.is_effective_submission = True
            submission.failure_reason_code = None
            db.flush()
            db.add(
                Feedback(
                    submission_id=submission.id,
                    evaluation_result=evaluation_result,
                    source=FeedbackSource.AUTO,
                    score_suggestion=evaluation_result.auto_score,
                    comment_text=(result.summary_json or {}).get("message", "Automatic evaluation completed."),
                )
            )
            evaluation_result.final_score = _recompute_notebook_final_score(submission, evaluation_result)
        else:
            task.status = EvaluationTaskStatus.FAILED
            task.error_message = result.error_message
            submission.completed_at = utcnow()
            if result.error_message and (
                "Docker is not installed" in result.error_message
                or "Runner finished without producing" in result.error_message
                or "Docker runner exited" in result.error_message
                or "system_error" == (result.summary_json or {}).get("failure_type")
            ):
                submission.status = SubmissionStatus.FAILED_SYSTEM
                submission.counts_toward_limit = False
                submission.is_effective_submission = False
                submission.failure_reason_code = "system_error"
            else:
                submission.status = SubmissionStatus.FAILED_ANSWER
                submission.counts_toward_limit = True
                submission.is_effective_submission = True
                submission.failure_reason_code = "answer_error"
            db.flush()
            db.add(
                Feedback(
                    submission_id=submission.id,
                    evaluation_result=evaluation_result,
                    source=FeedbackSource.AUTO,
                    score_suggestion=evaluation_result.auto_score,
                    comment_text=result.error_message or "Automatic evaluation failed.",
                )
            )
            evaluation_result.final_score = _recompute_notebook_final_score(submission, evaluation_result)

        db.commit()
        update_final_grade_snapshot(db, submission)
        if (
            submission.status in {SubmissionStatus.COMPLETED, SubmissionStatus.FAILED_ANSWER}
            and notebook_config
            and notebook_config.llm_feedback_enabled
            and _resolve_llm_config_for_question(question)
        ):
            with SessionLocal() as enqueue_db:
                enqueue_notebook_llm_feedback(enqueue_db, submission.id)
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error while processing submission %s", submission_id)
        submission = db.get(Submission, submission_id)
        task = db.get(EvaluationTask, task_id)
        if task is not None:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = utcnow()
            task.error_message = str(exc)
        if submission is not None:
            submission.status = SubmissionStatus.FAILED_SYSTEM
            submission.completed_at = utcnow()
            submission.counts_toward_limit = False
            submission.is_effective_submission = False
            submission.failure_reason_code = "system_error"
        db.commit()
    finally:
        db.close()


def process_python_code_evaluation(submission_id: int, task_id: int) -> None:
    db = SessionLocal()
    try:
        statement = (
            select(Submission)
            .options(
                joinedload(Submission.question).joinedload(Question.python_code_config),
                joinedload(Submission.assignment),
                joinedload(Submission.evaluation_results),
                joinedload(Submission.evaluation_tasks),
            )
            .where(Submission.id == submission_id)
        )
        submission = db.scalar(statement)
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None or not submission.stored_file_path:
            logger.error("Python code submission %s or task %s could not be loaded.", submission_id, task_id)
            return

        question = submission.question
        config = _python_code_config(question)
        if config is None:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = utcnow()
            task.error_message = "Python code question config is missing."
            submission.status = SubmissionStatus.FAILED_SYSTEM
            submission.completed_at = utcnow()
            submission.counts_toward_limit = False
            submission.is_effective_submission = False
            submission.failure_reason_code = "system_error"
            db.commit()
            return

        submission.status = SubmissionStatus.RUNNING
        submission.started_at = utcnow()
        task.status = EvaluationTaskStatus.RUNNING
        task.started_at = utcnow()
        task.error_message = None
        db.commit()

        output_dir = settings.outputs_dir / "submissions" / f"submission-{submission.id}"
        ensure_writable_directory(output_dir)
        runtime_image_tag, runtime_image = _resolve_runner_image_tag(question)
        task.runtime_image_id = runtime_image.id if runtime_image is not None else None
        result = run_python_code_in_docker(
            input_relative_path=submission.stored_file_path,
            output_dir_relative_path=relative_to_data(output_dir),
            runner_image=runtime_image_tag,
            timeout_seconds=config.time_limit_seconds,
            memory_limit=f"{config.memory_limit_mb}m",
            cpus=config.cpu_limit,
            network_disabled=not config.allow_network,
            visible_tests_json=json.dumps(config.visible_tests(), ensure_ascii=True),
            hidden_tests_json=json.dumps(config.hidden_tests(), ensure_ascii=True),
        )

        evaluation_result = EvaluationResult(
            submission_id=submission.id,
            evaluation_task_id=task.id,
            run_success=result.exit_code == 0,
            visible_score=Decimal(str((result.summary_json or {}).get("visible_score", 0))),
            hidden_score=Decimal(str((result.summary_json or {}).get("hidden_score", 0))),
            auto_score=Decimal(str((result.summary_json or {}).get("auto_score", 0))),
            final_score=Decimal(str((result.summary_json or {}).get("auto_score", 0))),
            log_path=relative_to_data(output_dir / "summary.json"),
            stdout_path=relative_to_data(output_dir / "stdout.txt"),
            stderr_path=relative_to_data(output_dir / "stderr.txt"),
            summary_json=json.dumps(result.summary_json or {}, ensure_ascii=True, indent=2),
        )
        db.add(evaluation_result)

        task.finished_at = utcnow()
        submission.completed_at = utcnow()
        if result.exit_code == 0:
            task.status = EvaluationTaskStatus.SUCCEEDED
            submission.status = SubmissionStatus.COMPLETED
            submission.counts_toward_limit = True
            submission.is_effective_submission = True
            submission.failure_reason_code = None
        else:
            task.status = EvaluationTaskStatus.FAILED
            task.error_message = result.error_message
            if result.error_message and "Docker is not installed" in result.error_message:
                submission.status = SubmissionStatus.FAILED_SYSTEM
                submission.counts_toward_limit = False
                submission.is_effective_submission = False
                submission.failure_reason_code = "system_error"
            else:
                submission.status = SubmissionStatus.FAILED_ANSWER
                submission.counts_toward_limit = True
                submission.is_effective_submission = True
                submission.failure_reason_code = "answer_error"

        db.flush()
        db.add(
            Feedback(
                submission_id=submission.id,
                evaluation_result=evaluation_result,
                source=FeedbackSource.AUTO,
                score_suggestion=evaluation_result.auto_score,
                comment_text=(result.summary_json or {}).get("message", "Python code evaluation completed."),
            )
        )
        db.commit()
        update_final_grade_snapshot(db, submission)
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error while processing Python code submission %s", submission_id)
        submission = db.get(Submission, submission_id)
        task = db.get(EvaluationTask, task_id)
        if task is not None:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = utcnow()
            task.error_message = str(exc)
        if submission is not None:
            submission.status = SubmissionStatus.FAILED_SYSTEM
            submission.completed_at = utcnow()
            submission.counts_toward_limit = False
            submission.is_effective_submission = False
            submission.failure_reason_code = "system_error"
        db.commit()
    finally:
        db.close()


def run_job_in_docker(
    *,
    input_relative_path: str,
    output_dir_relative_path: str,
    runner_image: str,
    timeout_seconds: int,
    memory_limit: str,
    cpus: str,
    network_disabled: bool,
    visible_tests_source: str,
    hidden_tests_source: str,
    execution_weight: str,
    visible_weight: str,
    hidden_weight: str,
) -> RunnerResult:
    input_path = absolute_data_path(input_relative_path)
    output_dir = absolute_data_path(output_dir_relative_path)
    runner_script_path = _runner_script_path("execute_notebook.py")
    executed_path = output_dir / "executed.ipynb"
    html_path = output_dir / "executed.html"
    stdout_path = output_dir / "stdout.txt"
    stderr_path = output_dir / "stderr.txt"
    summary_path = output_dir / "summary.json"
    ensure_writable_directory(output_dir)

    for artifact_path in (executed_path, html_path, stdout_path, stderr_path, summary_path):
        if artifact_path.exists():
            if artifact_path.is_file():
                artifact_path.unlink()
            else:
                shutil.rmtree(artifact_path)

    if not runner_script_path.exists():
        message = f"Runner helper script is missing from the application checkout: {runner_script_path}"
        write_text(stderr_path, f"{message}\n")
        summary = {"failure_type": "system_error", "message": message, "run_success": False, "auto_score": 0}
        summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
        return RunnerResult(exit_code=127, error_message=message, summary_json=summary)

    container_name = f"submission-runner-{uuid4().hex[:8]}"
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--memory",
        memory_limit,
        "--cpus",
        cpus,
        "--pids-limit",
        "256",
        "-v",
        f"{input_path.resolve().as_posix()}:/job/input.ipynb:ro",
        "-v",
        f"{output_dir.resolve().as_posix()}:/job/output",
        "-v",
        f"{runner_script_path.resolve().as_posix()}:/runner/execute_notebook.py:ro",
        "-w",
        "/job",
    ]
    if network_disabled:
        command.extend(["--network", "none"])

    command.extend(
        [
            runner_image,
            "--input",
            "/job/input.ipynb",
            "--executed",
            "/job/output/executed.ipynb",
            "--html",
            "/job/output/executed.html",
            "--stdout",
            "/job/output/stdout.txt",
            "--stderr",
            "/job/output/stderr.txt",
            "--summary",
            "/job/output/summary.json",
            "--visible-tests",
            visible_tests_source,
            "--hidden-tests",
            hidden_tests_source,
            "--execution-weight",
            execution_weight,
            "--visible-weight",
            visible_weight,
            "--hidden-weight",
            hidden_weight,
            "--timeout",
            str(timeout_seconds),
        ]
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _force_remove_container(container_name)
        message = f"Notebook execution timed out after {timeout_seconds} seconds."
        write_text(stderr_path, f"{message}\n")
        summary = {"failure_type": "answer_timeout", "message": message, "run_success": False, "auto_score": 0}
        summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
        return RunnerResult(exit_code=124, error_message=message, summary_json=summary)
    except FileNotFoundError:
        message = "Docker is not installed or is not available in PATH."
        write_text(stderr_path, f"{message}\n")
        summary = {"failure_type": "system_error", "message": message, "run_success": False, "auto_score": 0}
        summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
        return RunnerResult(exit_code=127, error_message=message, summary_json=summary)

    if completed.stdout.strip():
        write_text(stdout_path, f"{completed.stdout}\n", append=True)
    if completed.stderr.strip():
        write_text(stderr_path, f"{completed.stderr}\n", append=True)

    summary_json = {}
    if summary_path.exists():
        try:
            summary_json = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary_json = {}

    if completed.returncode != 0:
        message = summary_json.get("message") or "Docker runner exited with a non-zero status."
        summary_json.setdefault("failure_type", "answer_error")
        summary_json.setdefault("auto_score", 0)
        return RunnerResult(exit_code=completed.returncode, error_message=message, summary_json=summary_json)

    missing_artifacts = [
        name
        for name, path in {
            "executed notebook": executed_path,
            "html export": html_path,
            "stdout": stdout_path,
            "stderr": stderr_path,
            "summary": summary_path,
        }.items()
        if not path.exists()
    ]
    if missing_artifacts:
        message = f"Runner finished without producing required artifacts: {', '.join(missing_artifacts)}."
        write_text(stderr_path, f"{message}\n", append=True)
        summary_json.setdefault("failure_type", "system_error")
        summary_json.setdefault("message", message)
        summary_json.setdefault("auto_score", 0)
        summary_path.write_text(json.dumps(summary_json, ensure_ascii=True, indent=2), encoding="utf-8")
        return RunnerResult(exit_code=1, error_message=message, summary_json=summary_json)

    return RunnerResult(exit_code=0, summary_json=summary_json)


def run_python_code_in_docker(
    *,
    input_relative_path: str,
    output_dir_relative_path: str,
    runner_image: str,
    timeout_seconds: int,
    memory_limit: str,
    cpus: str,
    network_disabled: bool,
    visible_tests_json: str,
    hidden_tests_json: str,
) -> RunnerResult:
    input_path = absolute_data_path(input_relative_path)
    output_dir = absolute_data_path(output_dir_relative_path)
    runner_script_path = _runner_script_path("execute_python_code.py")
    stdout_path = output_dir / "stdout.txt"
    stderr_path = output_dir / "stderr.txt"
    summary_path = output_dir / "summary.json"
    ensure_writable_directory(output_dir)

    for artifact_path in (stdout_path, stderr_path, summary_path):
        if artifact_path.exists():
            artifact_path.unlink()

    if not runner_script_path.exists():
        message = f"Runner helper script is missing from the application checkout: {runner_script_path}"
        write_text(stderr_path, f"{message}\n")
        summary = {"failure_type": "system_error", "message": message, "run_success": False, "auto_score": 0}
        summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
        return RunnerResult(exit_code=127, error_message=message, summary_json=summary)

    container_name = f"python-submission-runner-{uuid4().hex[:8]}"
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--memory",
        memory_limit,
        "--cpus",
        cpus,
        "--pids-limit",
        "256",
        "--entrypoint",
        "python",
        "-v",
        f"{input_path.resolve().as_posix()}:/job/input.py:ro",
        "-v",
        f"{output_dir.resolve().as_posix()}:/job/output",
        "-v",
        f"{runner_script_path.resolve().as_posix()}:/runner/execute_python_code.py:ro",
        "-w",
        "/job",
    ]
    if network_disabled:
        command.extend(["--network", "none"])

    command.extend(
        [
            runner_image,
            "/runner/execute_python_code.py",
            "--input",
            "/job/input.py",
            "--stdout",
            "/job/output/stdout.txt",
            "--stderr",
            "/job/output/stderr.txt",
            "--summary",
            "/job/output/summary.json",
            "--visible-tests",
            visible_tests_json,
            "--hidden-tests",
            hidden_tests_json,
            "--timeout",
            str(timeout_seconds),
        ]
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds * 8 + 30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _force_remove_container(container_name)
        message = f"Python code execution timed out after {timeout_seconds} seconds per test."
        write_text(stderr_path, f"{message}\n")
        summary = {"failure_type": "answer_timeout", "message": message, "run_success": False, "auto_score": 0}
        summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
        return RunnerResult(exit_code=124, error_message=message, summary_json=summary)
    except FileNotFoundError:
        message = "Docker is not installed or is not available in PATH."
        write_text(stderr_path, f"{message}\n")
        summary = {"failure_type": "system_error", "message": message, "run_success": False, "auto_score": 0}
        summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
        return RunnerResult(exit_code=127, error_message=message, summary_json=summary)

    if completed.stdout.strip():
        write_text(stdout_path, f"{completed.stdout}\n", append=True)
    if completed.stderr.strip():
        write_text(stderr_path, f"{completed.stderr}\n", append=True)

    summary_json = {}
    if summary_path.exists():
        try:
            summary_json = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary_json = {}

    if completed.returncode != 0:
        message = summary_json.get("message") or "Python code runner exited with a non-zero status."
        summary_json.setdefault("failure_type", "answer_error")
        summary_json.setdefault("auto_score", 0)
        return RunnerResult(exit_code=completed.returncode, error_message=message, summary_json=summary_json)

    missing_artifacts = [name for name, path in {"stdout": stdout_path, "stderr": stderr_path, "summary": summary_path}.items() if not path.exists()]
    if missing_artifacts:
        message = f"Runner finished without producing required artifacts: {', '.join(missing_artifacts)}."
        write_text(stderr_path, f"{message}\n", append=True)
        summary_json.setdefault("failure_type", "system_error")
        summary_json.setdefault("message", message)
        summary_json.setdefault("auto_score", 0)
        summary_path.write_text(json.dumps(summary_json, ensure_ascii=True, indent=2), encoding="utf-8")
        return RunnerResult(exit_code=1, error_message=message, summary_json=summary_json)

    return RunnerResult(exit_code=0, summary_json=summary_json)


def process_short_answer_llm_evaluation(submission_id: int, task_id: int) -> None:
    db = SessionLocal()
    try:
        submission = db.scalar(
            select(Submission)
            .options(joinedload(Submission.question).joinedload(Question.short_answer_config), joinedload(Submission.assignment).joinedload(Assignment.course))
            .where(Submission.id == submission_id)
        )
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None:
            return

        llm_config = _resolve_llm_config_for_question(submission.question)
        if llm_config is None or not llm_config.enabled:
            task.status = EvaluationTaskStatus.FAILED
            task.error_message = "No enabled LLM config available."
            task.finished_at = utcnow()
            db.commit()
            return

        task.status = EvaluationTaskStatus.RUNNING
        task.started_at = utcnow()
        db.commit()

        result = generate_short_answer_evaluation(
            llm_config,
            question_title=submission.question.title,
            question_description=submission.question.description or "",
            rubric_text=submission.question.short_answer_config.rubric_text if submission.question.short_answer_config else "",
            reference_answer_text="",
            answer_text=submission.answer_text or "",
            max_score=float(submission.question.max_score),
        )
        db.add(
            Feedback(
                submission_id=submission.id,
                source=FeedbackSource.LLM,
                score_suggestion=Decimal(str(result.get("score_suggestion", 0))),
                comment_text=result.get("comment_text"),
            )
        )
        task.status = EvaluationTaskStatus.SUCCEEDED
        task.finished_at = utcnow()
        db.commit()
        if not submission_requires_teacher_confirmation(submission):
            refresh_final_grade_snapshot(db, submission.question_id, submission.user_id)
    except Exception as exc:
        logger.exception("Short-answer LLM evaluation failed for submission %s", submission_id)
        task = db.get(EvaluationTask, task_id)
        if task is not None:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = utcnow()
            task.error_message = str(exc)
            db.commit()
    finally:
        db.close()


def process_file_llm_evaluation(submission_id: int, task_id: int) -> None:
    db = SessionLocal()
    try:
        submission = db.scalar(
            select(Submission)
            .options(
                joinedload(Submission.question).joinedload(Question.file_question_config),
                joinedload(Submission.assignment).joinedload(Assignment.course),
            )
            .where(Submission.id == submission_id)
        )
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None:
            return

        llm_config = _resolve_llm_config_for_question(submission.question)
        question_config = _file_question_config(submission.question)
        if llm_config is None or not llm_config.enabled or question_config is None:
            task.status = EvaluationTaskStatus.FAILED
            task.error_message = "No enabled LLM config or file question config available."
            task.finished_at = utcnow()
            db.commit()
            return

        task.status = EvaluationTaskStatus.RUNNING
        task.started_at = utcnow()
        db.commit()

        result = generate_short_answer_evaluation(
            llm_config,
            question_title=submission.question.title,
            question_description=submission.question.description or "",
            rubric_text=question_config.rubric_text,
            reference_answer_text=question_config.reference_answer_text,
            answer_text=submission.answer_text or "",
            max_score=float(submission.question.max_score),
        )

        db.add(
            Feedback(
                submission_id=submission.id,
                source=FeedbackSource.LLM,
                score_suggestion=Decimal(str(result.get("score_suggestion", 0))),
                comment_text=result.get("comment_text"),
            )
        )
        if not question_config.teacher_confirmation_required:
            submission.status = SubmissionStatus.COMPLETED
            submission.completed_at = utcnow()
            submission.is_effective_submission = True
            submission.failure_reason_code = None
        task.status = EvaluationTaskStatus.SUCCEEDED
        task.finished_at = utcnow()
        db.commit()
        refresh_final_grade_snapshot(db, submission.question_id, submission.user_id)
    except Exception as exc:
        logger.exception("File LLM evaluation failed for submission %s", submission_id)
        task = db.get(EvaluationTask, task_id)
        if task is not None:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = utcnow()
            task.error_message = str(exc)
            db.commit()
    finally:
        db.close()


def process_notebook_llm_feedback(submission_id: int, task_id: int) -> None:
    db = SessionLocal()
    try:
        submission = db.scalar(
            select(Submission)
            .options(
                joinedload(Submission.question).joinedload(Question.notebook_config),
                joinedload(Submission.assignment).joinedload(Assignment.course),
                joinedload(Submission.evaluation_results),
            )
            .where(Submission.id == submission_id)
        )
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None:
            return

        llm_config = _resolve_llm_config_for_question(submission.question)
        latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
        if llm_config is None or latest_result is None or not llm_config.enabled:
            task.status = EvaluationTaskStatus.FAILED
            task.error_message = "No enabled LLM config or evaluation result available."
            task.finished_at = utcnow()
            db.commit()
            return

        task.status = EvaluationTaskStatus.RUNNING
        task.started_at = utcnow()
        db.commit()

        notebook_config = submission.question.notebook_config
        llm_max_score = Decimal(str(notebook_config.llm_score_weight if notebook_config else 0))
        stdout_text = read_submission_artifact_text(latest_result, "stdout")
        stderr_text = read_submission_artifact_text(latest_result, "stderr")
        llm_result = generate_notebook_evaluation_with_llm(
            llm_config,
            question_title=submission.question.title,
            question_description=submission.question.description or "",
            rubric_text=notebook_config.llm_scoring_rubric if notebook_config else "",
            summary_json=latest_result.summary_json or "{}",
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            max_llm_score=float(llm_max_score),
        )
        llm_score_value = Decimal(str(llm_result.get("score_suggestion", 0)))
        llm_score_value = _clamp_score(llm_score_value, Decimal("0"), llm_max_score)
        db.add(
            Feedback(
                submission_id=submission.id,
                evaluation_result_id=latest_result.id,
                source=FeedbackSource.LLM,
                score_suggestion=llm_score_value,
                comment_text=llm_result.get("comment_text"),
            )
        )
        latest_result.final_score = _recompute_notebook_final_score(submission, latest_result)
        task.status = EvaluationTaskStatus.SUCCEEDED
        task.finished_at = utcnow()
        db.commit()
        refresh_final_grade_snapshot(db, submission.question_id, submission.user_id)
    except Exception as exc:
        logger.exception("Notebook LLM feedback failed for submission %s", submission_id)
        task = db.get(EvaluationTask, task_id)
        if task is not None:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = utcnow()
            task.error_message = str(exc)
            db.commit()
    finally:
        db.close()


def _force_remove_container(container_name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:  # pragma: no cover
        logger.warning("Failed to force-remove timed out container %s", container_name)


def update_final_grade_snapshot(db: Session, submission: Submission) -> None:
    question = submission.question
    if question is None:
        question = db.get(Question, submission.question_id)
        submission.question = question
    if question is None:
        return

    rule = _question_scoring_rule(question)
    statement = (
        select(Submission)
        .options(joinedload(Submission.evaluation_results), joinedload(Submission.feedback_items))
        .where(
            Submission.question_id == submission.question_id,
            Submission.user_id == submission.user_id,
            Submission.is_effective_submission.is_(True),
        )
        .order_by(Submission.submitted_at.asc())
    )
    submissions = list(db.scalars(statement).unique())
    effective: Submission | None = None
    effective_score = Decimal("0")
    feedback_source = None

    def score_for(item: Submission) -> Decimal:
        resolved_score, _ = resolve_submission_score(item)
        return resolved_score if resolved_score is not None else Decimal("0")

    for item in submissions:
        current_score = score_for(item)
        if effective is None:
            effective = item
            effective_score = current_score
            continue
        if rule == ScoringRule.LATEST:
            if item.submitted_at >= effective.submitted_at:
                effective = item
                effective_score = current_score
        elif current_score >= effective_score:
            effective = item
            effective_score = current_score

    if effective is not None:
        _, feedback_source = resolve_submission_score(effective)

    snapshot = db.scalar(
        select(FinalGradeSnapshot).where(
            FinalGradeSnapshot.student_id == submission.user_id,
            FinalGradeSnapshot.question_id == submission.question_id,
        )
    )
    if snapshot is None:
        snapshot = FinalGradeSnapshot(
            student_id=submission.user_id,
            assignment_id=submission.assignment_id,
            question_id=submission.question_id,
            grading_rule_applied=rule,
        )
        db.add(snapshot)

    snapshot.effective_submission_id = effective.id if effective else None
    snapshot.grading_rule_applied = rule
    snapshot.score = effective_score if effective else None
    snapshot.feedback_source = feedback_source
    snapshot.updated_at = utcnow()
    db.commit()


def get_submission_artifact_path(submission: Submission, artifact_name: str) -> Path:
    result = submission.evaluation_results[-1] if submission.evaluation_results else None
    if result is None:
        raise FileNotFoundError("Submission artifacts are not available yet.")

    mapping = {
        "executed_notebook": result.executed_notebook_path,
        "html": result.rendered_html_path,
        "stdout": result.stdout_path,
        "stderr": result.stderr_path,
        "summary": result.log_path,
    }
    relative_path = mapping.get(artifact_name)
    if not relative_path:
        raise FileNotFoundError("Unknown artifact.")
    artifact_path = absolute_data_path(relative_path)
    if not artifact_path.exists():
        raise FileNotFoundError("Artifact file does not exist.")
    return artifact_path


def read_submission_text_artifact(submission: Submission, artifact_name: str, max_chars: int = 200000) -> str:
    try:
        artifact_path = get_submission_artifact_path(submission, artifact_name)
    except FileNotFoundError:
        return ""
    content = artifact_path.read_text(encoding="utf-8", errors="replace")
    if len(content) > max_chars:
        return f"{content[:max_chars]}\n\n... output truncated in web view ..."
    return content
