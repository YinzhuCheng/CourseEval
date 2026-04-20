import json
import logging
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import fitz
from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy import and_, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.constants import (
    EvaluationTaskStatus,
    EvaluationTaskType,
    FeedbackSource,
    AssignmentStatus,
    CodeLanguage,
    CodeSubmissionMode,
    MembershipStatus,
    QuestionType,
    ScoringRule,
    SubmissionLimitMode,
    SubmissionStatus,
)
from app.db import SessionLocal, utcnow
from app.services.llm import (
    ImageInput,
    generate_file_evaluation_from_images,
    generate_short_answer_evaluation,
    validate_grading_result_dict,
)
from app.services.llm_groups import call_llm_group, group_has_callable_target, latest_platform_llm_group
from app.services.notebook_multimodal import notebook_placeholder_alignment_block, sanitize_notebook_for_llm
from app.services.scoring import (
    is_submission_pending_teacher_review,
    resolve_submission_score,
    submission_eligible_for_gradebook,
    submission_requires_teacher_confirmation,
)
from app.services.storage_paths import (
    absolute_data_path,
    ensure_parent_dir,
    ensure_writable_directory,
    relative_to_data,
)
from app.services.user_storage import (
    QuotaExceededError,
    can_add_bytes,
    record_stored_object,
    submission_extra_paths,
    unlink_file_disk,
)
from app.models import (
    Assignment,
    Course,
    CourseMember,
    EvaluationResult,
    EvaluationTask,
    FileQuestionConfig,
    Feedback,
    FinalGradeSnapshot,
    CodeQuestionConfig,
    LLMConfig,
    Question,
    QuestionVersion,
    RuntimeImage,
    Submission,
)


logger = logging.getLogger(__name__)
settings = get_settings()
CODE_EVALUATION_QUEUE = "code-evaluations"
LLM_EVALUATION_QUEUE_PREFIX = "llm-evaluation"


@dataclass
class RunnerResult:
    exit_code: int
    error_message: str | None = None
    summary_json: dict | None = None


def _resolve_llm_config_for_question(question: Question, db: Session | None = None) -> LLMConfig | None:
    question_level = question.llm_config
    if question_level is not None and group_has_callable_target(question_level):
        return question_level

    assignment_level = question.assignment.llm_config
    if assignment_level is not None and group_has_callable_target(assignment_level):
        return assignment_level

    course = question.assignment.course
    if not course.use_global_llm_default:
        course_level = course.default_llm_config
        if course_level is not None and group_has_callable_target(course_level):
            return course_level

    if db is None:
        return None
    return latest_platform_llm_group(db)


_HIDDEN_STDOUT_MARKER = "=== Hidden Tests ==="
_HIDDEN_STDERR_MARKER = "=== Hidden Test stderr ==="


def _parsed_summary_json(evaluation_result: EvaluationResult | None) -> dict:
    if evaluation_result is None or not evaluation_result.summary_json:
        return {}
    try:
        return json.loads(evaluation_result.summary_json)
    except json.JSONDecodeError:
        return {}


def _file_question_config(question: Question | None) -> FileQuestionConfig | None:
    return question.file_question_config if question is not None else None


def _code_config(question: Question | None) -> CodeQuestionConfig | None:
    return question.code_config if question is not None else None


def _submission_snapshot_payload(submission: Submission) -> dict:
    version = submission.question_version
    if version is None or not version.snapshot_json:
        return {}
    try:
        payload = json.loads(version.snapshot_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _submission_snapshot_config(submission: Submission, key: str) -> dict:
    cfg = _submission_snapshot_payload(submission).get(key)
    return cfg if isinstance(cfg, dict) else {}


def _tests_from_snapshot_json(raw: object, fallback: list[dict]) -> list[dict]:
    if not isinstance(raw, str):
        return fallback
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return fallback
    if not isinstance(parsed, list):
        return fallback
    return [item for item in parsed if isinstance(item, dict)]


def _bool_snapshot_value(value: object, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _int_snapshot_value(value: object, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _str_snapshot_value(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def _resolve_runtime_image_for_question(question: Question) -> RuntimeImage | None:
    return question.runtime_image or question.assignment.runtime_image or question.assignment.course.default_runtime_image


def _resolve_runner_image_tag(question: Question) -> tuple[str, RuntimeImage | None]:
    runtime_image = _resolve_runtime_image_for_question(question)
    if runtime_image is not None and runtime_image.image_tag:
        return runtime_image.image_tag, runtime_image
    return settings.runner_image, None


def _bounded_cpu_limit(question_cpu: str, runtime_cpu: str | None) -> str:
    if not runtime_cpu:
        return question_cpu
    try:
        question_value = Decimal(str(question_cpu))
        runtime_value = Decimal(str(runtime_cpu))
    except Exception:
        return question_cpu
    return str(min(question_value, runtime_value))


def _runner_limits_for_code_config(
    *,
    timeout_seconds: int,
    memory_limit_mb: int,
    cpu_limit: str,
    allow_network: bool,
    runtime_image: RuntimeImage | None,
) -> tuple[int, str, str, bool]:
    timeout_seconds = int(timeout_seconds)
    memory_limit_mb = int(memory_limit_mb)
    cpus = str(cpu_limit)
    network_disabled = not bool(allow_network)
    if runtime_image is not None:
        timeout_seconds = min(timeout_seconds, int(runtime_image.timeout_seconds))
        memory_limit_mb = min(memory_limit_mb, int(runtime_image.memory_limit_mb))
        cpus = _bounded_cpu_limit(cpus, runtime_image.cpu_limit)
        network_disabled = network_disabled or not bool(runtime_image.network_enabled)
    return timeout_seconds, f"{memory_limit_mb}m", cpus, network_disabled


def _runner_script_path(script_name: str) -> Path:
    return settings.base_dir / "runner" / script_name


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


def redis_connection() -> Redis:
    return Redis.from_url(settings.redis_url)


def get_queue(queue_name: str) -> Queue:
    return Queue(queue_name, connection=redis_connection())


def get_code_queue_name() -> str:
    return settings.code_queue_name or CODE_EVALUATION_QUEUE


def llm_queue_name_for_config(config: LLMConfig) -> str:
    return f"{settings.llm_queue_prefix or LLM_EVALUATION_QUEUE_PREFIX}-{config.id}"


def _existing_backend_job_id(task: EvaluationTask) -> str | None:
    if task.status in {EvaluationTaskStatus.QUEUED, EvaluationTaskStatus.RUNNING} and task.backend_job_id:
        return task.backend_job_id
    return None


def _task_can_start(task: EvaluationTask) -> bool:
    return task.status == EvaluationTaskStatus.QUEUED


def _mark_submission_system_failed(
    db: Session,
    submission: Submission | None,
    task: EvaluationTask | None,
    message: str,
) -> None:
    finished_at = utcnow()
    if task is not None:
        task.status = EvaluationTaskStatus.FAILED
        task.finished_at = finished_at
        task.error_message = message
    if submission is not None and submission.status not in {
        SubmissionStatus.COMPLETED,
        SubmissionStatus.FAILED_ANSWER,
    }:
        submission.status = SubmissionStatus.FAILED_SYSTEM
        submission.completed_at = finished_at
        submission.counts_toward_limit = False
        submission.is_effective_submission = False
        submission.failure_reason_code = "system_error"
    db.commit()


def write_text(path: Path, content: str, append: bool = False) -> None:
    ensure_parent_dir(path)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as file_handle:
        file_handle.write(content)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


REFERENCE_ANSWER_UPLOAD_EXTENSIONS = frozenset({".pdf", ".tex", ".txt", ".md", ".ipynb"})


def store_reference_answer_file(
    *,
    user_id: int,
    question_id: int,
    original_filename: str,
    file_bytes: bytes,
) -> str:
    ext = Path(original_filename).suffix.lower()
    if ext not in REFERENCE_ANSWER_UPLOAD_EXTENSIONS:
        raise ValueError(f"Reference answer uploads must be one of: {', '.join(sorted(REFERENCE_ANSWER_UPLOAD_EXTENSIONS))}")
    relative, _ = _store_uploaded_file(
        user_id=user_id,
        question_id=question_id,
        original_filename=original_filename,
        file_bytes=file_bytes,
    )
    return relative


def _store_uploaded_file(*, user_id: int, question_id: int, original_filename: str, file_bytes: bytes) -> tuple[str, str]:
    suffix = Path(original_filename).suffix.lower() or ".bin"
    upload_dir = settings.uploads_dir / f"user-{user_id}" / f"question-{question_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = upload_dir / f"{uuid4().hex}{suffix}"
    stored_path.write_bytes(file_bytes)
    return relative_to_data(stored_path), stored_path.name


def _cleanup_submission_disk_files(relative_main: str) -> None:
    unlink_file_disk(relative_main)
    try:
        base = absolute_data_path(relative_main)
    except ValueError:
        return
    if base.suffix.lower() == ".pdf":
        pages_dir = base.parent / f"{base.stem}-pages"
        if pages_dir.is_dir():
            shutil.rmtree(pages_dir, ignore_errors=True)


def _register_submission_storage(db: Session, user_id: int, submission: Submission) -> None:
    if not submission.stored_file_path:
        return
    main_path = submission.stored_file_path
    try:
        main_sz = absolute_data_path(main_path).stat().st_size
        record_stored_object(
            db,
            user_id=user_id,
            category="submission",
            relative_path=main_path,
            size_bytes=main_sz,
            ref_type="submission",
            ref_id=submission.id,
        )
    except QuotaExceededError:
        raise
    for rel in submission_extra_paths(submission):
        try:
            sz = absolute_data_path(rel).stat().st_size
            record_stored_object(
                db,
                user_id=user_id,
                category="submission_extra",
                relative_path=rel,
                size_bytes=sz,
                ref_type="submission",
                ref_id=submission.id,
            )
        except QuotaExceededError:
            raise


def _ensure_text_file_extension(filename: str, allowed_extensions: set[str]) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in allowed_extensions:
        raise ValueError(f"File type {extension or '(none)'} is not allowed for this question.")
    return extension


def _render_pdf_pages_to_images(file_path: Path) -> list[Path]:
    pdf_document = fitz.open(file_path)
    if pdf_document.page_count <= 0:
        raise ValueError("The uploaded PDF is empty.")

    output_dir = file_path.parent / f"{file_path.stem}-pages"
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered_pages: list[Path] = []
    try:
        for page_index in range(min(pdf_document.page_count, settings.pdf_review_max_pages)):
            page = pdf_document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image_path = output_dir / f"page-{page_index + 1}.png"
            pixmap.save(image_path.as_posix())
            rendered_pages.append(image_path)
    finally:
        pdf_document.close()

    if not rendered_pages:
        raise ValueError("The uploaded PDF could not be rendered into images.")
    return rendered_pages


def _image_inputs_from_png_paths(paths: list[Path]) -> list[ImageInput]:
    return [ImageInput(mime_type="image/png", data=path.read_bytes()) for path in paths]


def _extract_pdf_text(file_path: Path, *, max_chars: int = 120000) -> str:
    doc = fitz.open(file_path)
    try:
        parts: list[str] = []
        for page_index in range(min(doc.page_count, settings.pdf_review_max_pages)):
            page = doc.load_page(page_index)
            parts.append(page.get_text("text") or "")
        text = "\n".join(parts).strip()
        return text[:max_chars] if len(text) > max_chars else text
    finally:
        doc.close()


def _merged_reference_answer_text(
    *,
    base_text: str,
    file_relative_path: str | None,
    notices: list[str],
) -> str:
    merged = (base_text or "").strip()
    if not file_relative_path:
        return merged
    path = absolute_data_path(file_relative_path)
    if not path.exists():
        notices.append("Reference answer file is missing on disk; using text field only.")
        return merged
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            extracted = _extract_pdf_text(path)
        elif ext in {".tex", ".txt", ".md"}:
            extracted = path.read_text(encoding="utf-8", errors="replace").strip()
        elif ext == ".ipynb":
            extracted = sanitize_notebook_for_llm(path, require_outputs=False).text.strip()
        else:
            notices.append(f"Unsupported reference answer file type {ext}; using text field only.")
            return merged
        if not extracted:
            notices.append("Reference answer file produced no extractable text; using text field only.")
            return merged
        if merged:
            return f"{merged}\n\n--- From uploaded reference file ---\n{extracted}"
        return extracted
    except Exception as exc:
        notices.append(f"Could not read reference answer file: {exc}")
        return merged


def _course_llm_response_language(question: Question) -> str | None:
    course = question.assignment.course
    return getattr(course, "llm_response_language", None) or "auto"


def _latest_feedback_comment(submission: Submission) -> str:
    items = [f for f in submission.feedback_items if f.source in {FeedbackSource.TEACHER, FeedbackSource.LLM}]
    if not items:
        return ""
    latest = max(items, key=lambda f: f.created_at)
    parts = []
    if latest.comment_text:
        parts.append(latest.comment_text.strip())
    if latest.score_suggestion is not None:
        parts.append(f"(suggested/recorded score: {latest.score_suggestion})")
    return "\n".join(parts).strip()


def _latest_teacher_score_text(submission: Submission) -> str:
    teacher_items = [f for f in submission.feedback_items if f.source == FeedbackSource.TEACHER]
    if not teacher_items:
        return ""
    latest = max(teacher_items, key=lambda f: f.created_at)
    if latest.score_suggestion is None:
        return ""
    return str(latest.score_suggestion)


def _submission_text_for_llm_context(submission: Submission) -> str:
    text = (submission.answer_text or "").strip()
    if text:
        return text
    return ""


def _find_previous_submission_with_feedback(db: Session, submission: Submission) -> Submission | None:
    statement = (
        select(Submission)
        .options(
            joinedload(Submission.feedback_items),
        )
        .where(
            Submission.question_id == submission.question_id,
            Submission.user_id == submission.user_id,
            Submission.submitted_at < submission.submitted_at,
        )
        .order_by(Submission.submitted_at.desc())
    )
    for prev in db.scalars(statement).unique():
        if any(f.source in {FeedbackSource.TEACHER, FeedbackSource.LLM} for f in prev.feedback_items):
            return prev
    return None


def _truncate_for_llm(label: str, text: str, max_len: int, notices: list[str]) -> str:
    if len(text) <= max_len:
        return text
    notices.append(f"{label} was truncated to {max_len} characters for the LLM prompt.")
    return text[:max_len] + "\n...[truncated]"


def _extract_formatted_text(file_path: Path, extension: str, *, require_ipynb_output: bool) -> str:
    if extension in {".txt", ".tex", ".md", ".py"}:
        return file_path.read_text(encoding="utf-8", errors="replace").strip()
    if extension == ".ipynb":
        return sanitize_notebook_for_llm(file_path, require_outputs=require_ipynb_output).text.strip()
    raise ValueError(f"Unsupported formatted-text file type: {extension}.")


def _parse_test_cases_json(raw_json: str) -> list[dict]:
    try:
        payload = json.loads(raw_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Stored code test cases are not valid JSON.") from exc
    if not isinstance(payload, list):
        raise ValueError("Stored code test cases must be a JSON list.")
    normalized: list[dict] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError("Each code test case must be a JSON object.")
        normalized.append(
            {
                "name": item.get("name") or f"Test {index}",
                "input": item.get("input", ""),
                "expected_output": item.get("expected_output", item.get("output", "")),
                "points": float(item.get("points", 20)),
            }
        )
    return normalized


def get_question_for_student(db: Session, question_id: int, user_id: int) -> Question | None:
    statement = (
        select(Question)
        .options(
            joinedload(Question.assignment).joinedload(Assignment.course),
            joinedload(Question.code_config),
            joinedload(Question.file_question_config),
            joinedload(Question.short_answer_config),
        )
        .join(Assignment, Question.assignment_id == Assignment.id)
        .join(CourseMember, and_(CourseMember.course_id == Assignment.course_id, CourseMember.user_id == user_id))
        .where(
            Question.id == question_id,
            Assignment.status == AssignmentStatus.PUBLISHED,
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
            joinedload(Submission.question).joinedload(Question.code_config),
            joinedload(Submission.question).joinedload(Question.file_question_config),
            joinedload(Submission.question).joinedload(Question.short_answer_config),
            joinedload(Submission.evaluation_tasks),
            joinedload(Submission.evaluation_results),
            joinedload(Submission.feedback_items),
            joinedload(Submission.question_version),
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
            joinedload(Submission.question).joinedload(Question.code_config),
            joinedload(Submission.question).joinedload(Question.file_question_config),
            joinedload(Submission.question).joinedload(Question.short_answer_config),
            joinedload(Submission.evaluation_tasks),
            joinedload(Submission.evaluation_results),
            joinedload(Submission.feedback_items),
            joinedload(Submission.question_version),
        )
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .join(Course, Course.id == Assignment.course_id)
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
    due_at = _as_utc(question.assignment.due_at)
    if due_at is None:
        return False
    return now > due_at


def _submission_window_open(question: Question) -> tuple[bool, str | None]:
    now = _now()
    assignment = question.assignment
    if assignment.status != AssignmentStatus.PUBLISHED:
        return False, "Assignment is not published."
    open_at = _as_utc(assignment.open_at)
    due_at = _as_utc(assignment.due_at)
    close_at = _as_utc(assignment.close_at)
    if open_at and now < open_at:
        return False, "Submission window has not opened yet."
    if close_at and now > close_at:
        return False, "Submission window is closed."
    if due_at and now > due_at and not assignment.allow_late:
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
        try:
            local_now = now.astimezone(ZoneInfo(settings.timezone_name))
        except ZoneInfoNotFoundError:
            local_now = now
        local_start = datetime(local_now.year, local_now.month, local_now.day, tzinfo=local_now.tzinfo)
        start_of_day = local_start.astimezone(timezone.utc)
        used = _count_attempts(db, user_id, question.id, start_of_day=start_of_day)
        if used >= limit:
            return False, f"You have already used the daily submission limit for this question ({limit})."
        return True, None

    return True, None


def _begin_submission_limit_write_lock(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "sqlite":
        return
    if db.in_transaction():
        db.commit()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    except OperationalError as exc:
        if "cannot start a transaction within a transaction" not in str(exc).lower():
            raise


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

    _begin_submission_limit_write_lock(db)
    allowed, message = _check_submission_limit(db, question, user_id)
    if not allowed:
        raise ValueError(message or "Submission limit reached.")

    config = question.short_answer_config
    teacher_confirmation_required = False if config is None else bool(config.teacher_confirmation_required)
    cleaned = answer_text.strip()
    if not cleaned:
        raise ValueError("Answer cannot be empty.")
    if config and config.min_length and len(cleaned) < config.min_length:
        raise ValueError(f"Answer must be at least {config.min_length} characters long.")
    if config and config.max_length and len(cleaned) > config.max_length:
        raise ValueError(f"Answer must be at most {config.max_length} characters long.")

    llm_will_run = bool(config and config.llm_suggestion_enabled)
    if llm_will_run:
        initial_status = SubmissionStatus.SUBMITTED
        initial_completed_at = None
        initial_effective = False
    else:
        initial_status = SubmissionStatus.SUBMITTED if teacher_confirmation_required else SubmissionStatus.COMPLETED
        initial_completed_at = None if teacher_confirmation_required else utcnow()
        initial_effective = not teacher_confirmation_required

    submission = Submission(
        course_id=question.assignment.course_id,
        assignment_id=question.assignment_id,
        question_id=question.id,
        user_id=user_id,
        submission_type=QuestionType.SHORT_ANSWER,
        status=initial_status,
        answer_text=cleaned,
        submitted_at=utcnow(),
        completed_at=initial_completed_at,
        is_late=_is_late(question),
        counts_toward_limit=True,
        is_effective_submission=initial_effective,
        question_version_id=question.current_question_version_id,
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
    if not llm_will_run and not teacher_confirmation_required:
        update_final_grade_snapshot(db, submission)
    if config and config.llm_suggestion_enabled:
        enqueue_short_answer_llm(db, submission.id)
    return submission


def _code_language_from_value(value: str | CodeLanguage) -> CodeLanguage:
    try:
        return value if isinstance(value, CodeLanguage) else CodeLanguage(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError("Unsupported code language.") from exc


def _code_submission_mode_for_filename(filename: str) -> CodeSubmissionMode:
    return CodeSubmissionMode.ZIP if Path(filename).suffix.lower() == ".zip" else CodeSubmissionMode.SINGLE_FILE


def _allowed_suffixes_for_language(language: CodeLanguage) -> set[str]:
    if language == CodeLanguage.PYTHON:
        return {".py", ".zip"}
    if language == CodeLanguage.C:
        return {".c", ".zip"}
    return {".cpp", ".cc", ".cxx", ".zip"}


def _read_code_preview(path: Path, language: CodeLanguage, mode: CodeSubmissionMode) -> str:
    if mode == CodeSubmissionMode.SINGLE_FILE:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    entrypoint = {
        CodeLanguage.PYTHON: "main.py",
        CodeLanguage.C: "main.c",
        CodeLanguage.CPP: "main.cpp",
    }[language]
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open(entrypoint) as source:
                return source.read(256 * 1024).decode("utf-8", errors="replace").strip()
    except Exception:
        return f"Multi-file {language.value} submission: {path.name}"


def create_code_submission(
    db: Session,
    *,
    user_id: int,
    question: Question,
    original_filename: str,
    submission_bytes: bytes,
    language: str | CodeLanguage,
) -> Submission:
    allowed, message = _submission_window_open(question)
    if not allowed:
        raise ValueError(message or "Submission window is closed.")

    _begin_submission_limit_write_lock(db)
    allowed, message = _check_submission_limit(db, question, user_id)
    if not allowed:
        raise ValueError(message or "Submission limit reached.")

    config = _code_config(question)
    if config is None:
        raise ValueError("Code question configuration is missing for this question.")
    code_language = _code_language_from_value(language)
    if not config.allows_language(code_language):
        raise ValueError("This language is not allowed for the question.")
    _ensure_text_file_extension(original_filename, _allowed_suffixes_for_language(code_language))
    submission_mode = _code_submission_mode_for_filename(original_filename)
    stored_relative_path, _ = _store_uploaded_file(
        user_id=user_id,
        question_id=question.id,
        original_filename=original_filename,
        file_bytes=submission_bytes,
    )
    main_sz = absolute_data_path(stored_relative_path).stat().st_size
    if not can_add_bytes(db, user_id, main_sz):
        _cleanup_submission_disk_files(stored_relative_path)
        raise ValueError("storage_quota_exceeded")
    source_text = _read_code_preview(absolute_data_path(stored_relative_path), code_language, submission_mode)
    if not source_text:
        raise ValueError("Uploaded code file is empty.")

    submission = Submission(
        course_id=question.assignment.course_id,
        assignment_id=question.assignment_id,
        question_id=question.id,
        user_id=user_id,
        submission_type=QuestionType.CODE,
        status=SubmissionStatus.SUBMITTED,
        original_filename=original_filename,
        stored_file_path=stored_relative_path,
        answer_text=source_text,
        code_language=code_language,
        code_submission_mode=submission_mode,
        submitted_at=utcnow(),
        is_late=_is_late(question),
        counts_toward_limit=True,
        is_effective_submission=False,
        question_version_id=question.current_question_version_id,
    )
    db.add(submission)
    db.flush()
    try:
        _register_submission_storage(db, user_id, submission)
    except QuotaExceededError:
        db.delete(submission)
        db.flush()
        _cleanup_submission_disk_files(stored_relative_path)
        raise ValueError("storage_quota_exceeded") from None

    db.add(
        EvaluationTask(
            submission_id=submission.id,
            task_type=EvaluationTaskType.CODE_EVALUATION,
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

    _begin_submission_limit_write_lock(db)
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
    pdf_page_paths: list[Path] = []
    use_pdf_pipeline = extension == ".pdf"
    if use_pdf_pipeline:
        pdf_page_paths = _render_pdf_pages_to_images(stored_path)
        extracted_text = f"PDF rendered into {len(pdf_page_paths)} page image(s) for multimodal LLM review."
    else:
        extracted_text = _extract_formatted_text(
            stored_path,
            extension,
            require_ipynb_output=config.notebook_outputs_required,
        )
    if not extracted_text:
        _cleanup_submission_disk_files(stored_relative_path)
        raise ValueError("The uploaded file does not contain any extractable content.")

    main_sz = stored_path.stat().st_size
    extra_sz = sum(p.stat().st_size for p in pdf_page_paths) if pdf_page_paths else 0
    if not can_add_bytes(db, user_id, main_sz + extra_sz):
        _cleanup_submission_disk_files(stored_relative_path)
        raise ValueError("storage_quota_exceeded")

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
        question_version_id=question.current_question_version_id,
    )
    db.add(submission)
    db.flush()
    try:
        _register_submission_storage(db, user_id, submission)
    except QuotaExceededError:
        db.delete(submission)
        db.flush()
        _cleanup_submission_disk_files(stored_relative_path)
        raise ValueError("storage_quota_exceeded") from None

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
    existing_job_id = _existing_backend_job_id(task)
    if existing_job_id:
        return existing_job_id

    if task.task_type != EvaluationTaskType.CODE_EVALUATION:
        raise ValueError("Only code evaluation tasks use the code runner queue.")
    target_func = process_code_evaluation
    timeout_seconds = settings.execution_timeout_seconds + 60

    try:
        rq_job = get_queue(get_code_queue_name()).enqueue(
            target_func,
            submission_id,
            task.id,
            job_timeout=timeout_seconds,
            result_ttl=86400,
            failure_ttl=86400,
        )
    except Exception as exc:
        _mark_submission_system_failed(db, submission, task, f"Failed to enqueue code evaluation: {exc}")
        raise
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
    existing_job_id = _existing_backend_job_id(task)
    if existing_job_id:
        return existing_job_id

    llm_group = _resolve_llm_config_for_question(submission.question, db)
    if llm_group is None:
        _mark_submission_system_failed(db, submission, task, "No enabled LLM group available.")
        raise ValueError("No enabled LLM group available.")
    try:
        rq_job = get_queue(llm_queue_name_for_config(llm_group)).enqueue(
            process_short_answer_llm_evaluation,
            submission_id,
            task.id,
            job_timeout=120,
            result_ttl=86400,
            failure_ttl=86400,
        )
    except Exception as exc:
        _mark_submission_system_failed(db, submission, task, f"Failed to enqueue short-answer LLM evaluation: {exc}")
        raise
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
    existing_job_id = _existing_backend_job_id(task)
    if existing_job_id:
        return existing_job_id

    llm_group = _resolve_llm_config_for_question(submission.question, db)
    if llm_group is None:
        _mark_submission_system_failed(db, submission, task, "No enabled LLM group available.")
        raise ValueError("No enabled LLM group available.")
    try:
        rq_job = get_queue(llm_queue_name_for_config(llm_group)).enqueue(
            process_file_llm_evaluation,
            submission_id,
            task.id,
            job_timeout=120,
            result_ttl=86400,
            failure_ttl=86400,
        )
    except Exception as exc:
        _mark_submission_system_failed(db, submission, task, f"Failed to enqueue file LLM evaluation: {exc}")
        raise
    task.backend_job_id = rq_job.id
    task.status = EvaluationTaskStatus.QUEUED
    db.commit()
    return rq_job.id


def _running_recovery_cutoff() -> datetime:
    """Do not treat short-lived RUNNING rows as stale (avoids false failures on worker restart)."""
    buffer_minutes = max(30, settings.execution_timeout_seconds // 60 + 10)
    return utcnow() - timedelta(minutes=buffer_minutes)


def cleanup_stale_running_items() -> int:
    with SessionLocal() as db:
        cutoff = _running_recovery_cutoff()
        started_or_submitted = func.coalesce(Submission.started_at, Submission.submitted_at)
        running_submissions = list(
            db.scalars(
                select(Submission).where(
                    Submission.status == SubmissionStatus.RUNNING,
                    started_or_submitted < cutoff,
                )
            ).all()
        )
        task_started_or_created = func.coalesce(EvaluationTask.started_at, EvaluationTask.created_at)
        running_tasks = list(
            db.scalars(
                select(EvaluationTask).where(
                    EvaluationTask.status == EvaluationTaskStatus.RUNNING,
                    task_started_or_created < cutoff,
                )
            ).all()
        )
        stale_cutoff = utcnow() - timedelta(minutes=10)
        unqueued_tasks = list(
            db.scalars(
                select(EvaluationTask).where(
                    EvaluationTask.status == EvaluationTaskStatus.QUEUED,
                    EvaluationTask.backend_job_id.is_(None),
                    EvaluationTask.created_at < stale_cutoff,
                )
            ).all()
        )

        finished_at = utcnow()
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

        for task in unqueued_tasks:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = finished_at
            task.error_message = "Evaluation task was created but never enqueued."
            if task.submission and task.submission.status in {SubmissionStatus.SUBMITTED, SubmissionStatus.QUEUED}:
                task.submission.status = SubmissionStatus.FAILED_SYSTEM
                task.submission.completed_at = finished_at
                task.submission.counts_toward_limit = False
                task.submission.is_effective_submission = False
                task.submission.failure_reason_code = "system_error"

        db.commit()
        return len(running_submissions) + len(running_tasks) + len(unqueued_tasks)


def cleanup_missing_queued_jobs() -> int:
    with SessionLocal() as db:
        tasks = list(
            db.scalars(
                select(EvaluationTask)
                .options(joinedload(EvaluationTask.submission))
                .where(
                    EvaluationTask.status == EvaluationTaskStatus.QUEUED,
                    EvaluationTask.backend_job_id.is_not(None),
                )
            ).unique()
        )
        if not tasks:
            return 0
        connection = redis_connection()
        finished_at = utcnow()
        changed = 0
        for task in tasks:
            try:
                Job.fetch(str(task.backend_job_id), connection=connection)
            except NoSuchJobError:
                task.status = EvaluationTaskStatus.FAILED
                task.finished_at = finished_at
                task.error_message = "Queued backend job is missing from Redis."
                if task.submission and task.submission.status in {SubmissionStatus.SUBMITTED, SubmissionStatus.QUEUED}:
                    task.submission.status = SubmissionStatus.FAILED_SYSTEM
                    task.submission.completed_at = finished_at
                    task.submission.counts_toward_limit = False
                    task.submission.is_effective_submission = False
                    task.submission.failure_reason_code = "system_error"
                changed += 1
        if changed:
            db.commit()
        return changed


def process_code_evaluation(submission_id: int, task_id: int) -> None:
    db = SessionLocal()
    try:
        statement = (
            select(Submission)
            .options(
                joinedload(Submission.question).joinedload(Question.code_config),
                joinedload(Submission.assignment),
                joinedload(Submission.evaluation_results),
                joinedload(Submission.evaluation_tasks),
                joinedload(Submission.question_version),
            )
            .where(Submission.id == submission_id)
        )
        submission = db.scalar(statement)
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None or not submission.stored_file_path:
            logger.error("Code submission %s or task %s could not be loaded.", submission_id, task_id)
            return
        if not _task_can_start(task):
            logger.info("Skipping code task %s because it is already %s.", task_id, task.status.value)
            return

        question = submission.question
        config = _code_config(question)
        if config is None:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = utcnow()
            task.error_message = "Code question config is missing."
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
        snapshot_config = _submission_snapshot_config(submission, "code_config")
        visible_tests = _tests_from_snapshot_json(snapshot_config.get("visible_tests_json"), config.visible_tests())
        hidden_tests = _tests_from_snapshot_json(snapshot_config.get("hidden_tests_json"), config.hidden_tests())
        time_limit_seconds = _int_snapshot_value(snapshot_config.get("time_limit_seconds"), config.time_limit_seconds)
        memory_limit_mb = _int_snapshot_value(snapshot_config.get("memory_limit_mb"), config.memory_limit_mb)
        cpu_limit = _str_snapshot_value(snapshot_config.get("cpu_limit"), config.cpu_limit)
        allow_network = _bool_snapshot_value(snapshot_config.get("allow_network"), config.allow_network)
        runner_timeout, runner_memory, runner_cpus, runner_network_disabled = _runner_limits_for_code_config(
            timeout_seconds=time_limit_seconds,
            memory_limit_mb=memory_limit_mb,
            cpu_limit=cpu_limit,
            allow_network=allow_network,
            runtime_image=runtime_image,
        )
        result = run_code_in_docker(
            input_relative_path=submission.stored_file_path,
            language=(submission.code_language or CodeLanguage.PYTHON).value,
            submission_mode=(submission.code_submission_mode or CodeSubmissionMode.SINGLE_FILE).value,
            output_dir_relative_path=relative_to_data(output_dir),
            runner_image=runtime_image_tag,
            timeout_seconds=runner_timeout,
            memory_limit=runner_memory,
            cpus=runner_cpus,
            network_disabled=runner_network_disabled,
            visible_tests_json=json.dumps(visible_tests, ensure_ascii=True),
            hidden_tests_json=json.dumps(hidden_tests, ensure_ascii=True),
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
                comment_text=(result.summary_json or {}).get("message", "Code evaluation completed."),
            )
        )
        db.commit()
        update_final_grade_snapshot(db, submission)
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error while processing code submission %s", submission_id)
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


def run_code_in_docker(
    *,
    input_relative_path: str,
    language: str,
    submission_mode: str,
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
    runner_script_path = _runner_script_path("execute_code.py")
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

    container_name = f"code-submission-runner-{uuid4().hex[:8]}"
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
        f"{input_path.resolve().as_posix()}:/job/input{submission_mode == 'zip' and '.zip' or Path(input_path).suffix}:ro",
        "-v",
        f"{output_dir.resolve().as_posix()}:/job/output",
        "-v",
        f"{runner_script_path.resolve().as_posix()}:/runner/execute_code.py:ro",
        "-w",
        "/job",
    ]
    if network_disabled:
        command.extend(["--network", "none"])

    command.extend(
        [
            runner_image,
            "/runner/execute_code.py",
            "--input",
            f"/job/input{submission_mode == 'zip' and '.zip' or Path(input_path).suffix}",
            "--language",
            language,
            "--submission-mode",
            submission_mode,
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
        message = f"Code execution timed out after {timeout_seconds} seconds per test."
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
        message = summary_json.get("message") or "Code runner exited with a non-zero status."
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
            .options(
                joinedload(Submission.question).joinedload(Question.short_answer_config),
                joinedload(Submission.assignment).joinedload(Assignment.course),
                joinedload(Submission.question_version),
            )
            .where(Submission.id == submission_id)
        )
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None:
            return
        if not _task_can_start(task):
            logger.info("Skipping short-answer LLM task %s because it is already %s.", task_id, task.status.value)
            return

        llm_group = _resolve_llm_config_for_question(submission.question, db)
        if llm_group is None or not group_has_callable_target(llm_group):
            _mark_submission_system_failed(db, submission, task, "No enabled LLM group available.")
            return

        task.status = EvaluationTaskStatus.RUNNING
        task.started_at = utcnow()
        db.commit()

        notices: list[str] = []
        snapshot_config = _submission_snapshot_config(submission, "short_answer_config")
        rubric_text = _str_snapshot_value(
            snapshot_config.get("rubric_text"),
            submission.question.short_answer_config.rubric_text if submission.question.short_answer_config else "",
        )
        prev = _find_previous_submission_with_feedback(db, submission)
        prev_answer = _submission_text_for_llm_context(prev) if prev else ""
        prev_fb = _latest_feedback_comment(prev) if prev else ""
        prev_score = _latest_teacher_score_text(prev) if prev else ""
        trunc_notice = ""
        answer_text = _truncate_for_llm("Student answer", submission.answer_text or "", 24000, notices)
        if notices:
            trunc_notice = " ".join(notices)

        def _run_sa(llm_target) -> dict:
            return generate_short_answer_evaluation(
                llm_target,
                question_title=submission.question.title,
                question_description=submission.question.description or "",
                rubric_text=rubric_text,
                reference_answer_text="",
                answer_text=answer_text,
                max_score=float(submission.question.max_score),
                previous_submission_text=prev_answer,
                previous_feedback_text=prev_fb,
                previous_teacher_score_text=prev_score,
                truncation_notice=trunc_notice,
                course_llm_response_language=_course_llm_response_language(submission.question),
                text_format_may_lose_images=False,
                bill_user_id=submission.user_id,
                bill_db=db,
            )

        result = call_llm_group(llm_group, _run_sa, label="short_answer_llm")
        result = validate_grading_result_dict(result, max_score=float(submission.question.max_score))
        comment = result.get("comment_text") or ""
        if notices:
            comment = (
                comment
                + "\n\n"
                + "\n".join(f"[Grading system notice] {item}" for item in notices)
            ).strip()
        db.add(
            Feedback(
                submission_id=submission.id,
                source=FeedbackSource.LLM,
                score_suggestion=Decimal(str(result.get("score_suggestion", 0))),
                comment_text=comment,
            )
        )
        submission.status = SubmissionStatus.COMPLETED
        submission.completed_at = utcnow()
        submission.is_effective_submission = not submission_requires_teacher_confirmation(submission)
        submission.failure_reason_code = None
        task.status = EvaluationTaskStatus.SUCCEEDED
        task.finished_at = utcnow()
        db.commit()
        refresh_final_grade_snapshot(db, submission.question_id, submission.user_id)
    except Exception as exc:
        logger.exception("Short-answer LLM evaluation failed for submission %s", submission_id)
        submission = db.get(Submission, submission_id)
        task = db.get(EvaluationTask, task_id)
        _mark_submission_system_failed(db, submission, task, str(exc))
        raise
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
                joinedload(Submission.question_version),
            )
            .where(Submission.id == submission_id)
        )
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None:
            return
        if not _task_can_start(task):
            logger.info("Skipping file LLM task %s because it is already %s.", task_id, task.status.value)
            return
        if not submission.stored_file_path:
            _mark_submission_system_failed(
                db,
                submission,
                task,
                "Submission file was removed (storage purge); scores are unchanged.",
            )
            return

        llm_group = _resolve_llm_config_for_question(submission.question, db)
        question_config = _file_question_config(submission.question)
        if llm_group is None or not group_has_callable_target(llm_group) or question_config is None:
            _mark_submission_system_failed(db, submission, task, "No enabled LLM group or file question config available.")
            return

        task.status = EvaluationTaskStatus.RUNNING
        task.started_at = utcnow()
        db.commit()

        notices: list[str] = []
        snapshot_config = _submission_snapshot_config(submission, "file_question_config")
        reference_answer_text = _str_snapshot_value(
            snapshot_config.get("reference_answer_text"),
            question_config.reference_answer_text,
        )
        reference_answer_file_path = snapshot_config.get("reference_answer_file_path")
        if not isinstance(reference_answer_file_path, str):
            reference_answer_file_path = question_config.reference_answer_file_path
        rubric_text = _str_snapshot_value(snapshot_config.get("rubric_text"), question_config.rubric_text)
        notebook_outputs_required = _bool_snapshot_value(
            snapshot_config.get("notebook_outputs_required"),
            question_config.notebook_outputs_required,
        )
        ref_merged = _merged_reference_answer_text(
            base_text=reference_answer_text,
            file_relative_path=reference_answer_file_path,
            notices=notices,
        )
        prev = _find_previous_submission_with_feedback(db, submission)
        prev_answer = _submission_text_for_llm_context(prev) if prev else ""
        prev_fb = _latest_feedback_comment(prev) if prev else ""
        prev_score = _latest_teacher_score_text(prev) if prev else ""
        trunc_notice = ""
        prev_answer = _truncate_for_llm("Previous submission", prev_answer, 12000, notices)
        prev_fb = _truncate_for_llm("Previous feedback", prev_fb, 8000, notices)
        ref_for_prompt = _truncate_for_llm("Reference answer", ref_merged, 24000, notices)
        if notices:
            trunc_notice = " ".join(notices)

        ext = Path(submission.original_filename or "").suffix.lower()
        if submission.stored_file_path and ext == ".pdf":
            pdf_path = absolute_data_path(submission.stored_file_path)
            page_dir = pdf_path.parent / f"{pdf_path.stem}-pages"
            page_paths = sorted(page_dir.glob("page-*.png"))
            if not page_paths:
                page_paths = _render_pdf_pages_to_images(pdf_path)

            def _run_pdf(llm_target) -> dict:
                return generate_file_evaluation_from_images(
                    llm_target,
                    question_title=submission.question.title,
                    question_description=submission.question.description or "",
                    rubric_text=rubric_text,
                    reference_answer_text=ref_for_prompt,
                    max_score=float(submission.question.max_score),
                    images=_image_inputs_from_png_paths(page_paths),
                    previous_submission_text=prev_answer,
                    previous_feedback_text=prev_fb,
                    previous_teacher_score_text=prev_score,
                    truncation_notice=trunc_notice,
                    course_llm_response_language=_course_llm_response_language(submission.question),
                    bill_user_id=submission.user_id,
                    bill_db=db,
                )

            result = call_llm_group(llm_group, _run_pdf, label="file_llm_pdf")
            result = validate_grading_result_dict(result, max_score=float(submission.question.max_score))
        else:
            ans = _truncate_for_llm("Student answer", submission.answer_text or "", 24000, notices)
            if notices and not trunc_notice:
                trunc_notice = " ".join(notices)
            notebook_images = None
            notebook_instructions = ""
            if ext == ".ipynb" and submission.stored_file_path:
                nb_mm = sanitize_notebook_for_llm(
                    absolute_data_path(submission.stored_file_path),
                    require_outputs=notebook_outputs_required,
                )
                notebook_images = nb_mm.images or None
                notebook_instructions = notebook_placeholder_alignment_block(nb_mm.registry, len(nb_mm.images))

            def _run_text(llm_target) -> dict:
                return generate_short_answer_evaluation(
                    llm_target,
                    question_title=submission.question.title,
                    question_description=submission.question.description or "",
                    rubric_text=rubric_text,
                    reference_answer_text=ref_for_prompt,
                    answer_text=ans,
                    max_score=float(submission.question.max_score),
                    previous_submission_text=prev_answer,
                    previous_feedback_text=prev_fb,
                    previous_teacher_score_text=prev_score,
                    truncation_notice=trunc_notice,
                    course_llm_response_language=_course_llm_response_language(submission.question),
                    text_format_may_lose_images=ext in {".tex", ".ipynb", ".txt"},
                    bill_user_id=submission.user_id,
                    bill_db=db,
                    images=notebook_images,
                    multimodal_instructions=notebook_instructions,
                )

            result = call_llm_group(llm_group, _run_text, label="file_llm_text")
            result = validate_grading_result_dict(result, max_score=float(submission.question.max_score))

        comment = result.get("comment_text") or ""
        if notices:
            comment = (
                comment + "\n\n" + "\n".join(f"[Grading system notice] {item}" for item in notices)
            ).strip()
        db.add(
            Feedback(
                submission_id=submission.id,
                source=FeedbackSource.LLM,
                score_suggestion=Decimal(str(result.get("score_suggestion", 0))),
                comment_text=comment,
            )
        )
        submission.status = SubmissionStatus.COMPLETED
        submission.completed_at = utcnow()
        submission.is_effective_submission = not submission_requires_teacher_confirmation(submission)
        submission.failure_reason_code = None
        task.status = EvaluationTaskStatus.SUCCEEDED
        task.finished_at = utcnow()
        db.commit()
        refresh_final_grade_snapshot(db, submission.question_id, submission.user_id)
    except Exception as exc:
        logger.exception("File LLM evaluation failed for submission %s", submission_id)
        submission = db.get(Submission, submission_id)
        task = db.get(EvaluationTask, task_id)
        _mark_submission_system_failed(db, submission, task, str(exc))
        raise
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
        )
        .order_by(Submission.submitted_at.asc())
    )
    all_submissions = list(db.scalars(statement).unique())
    submissions = [item for item in all_submissions if submission_eligible_for_gradebook(item)]
    effective: Submission | None = None
    effective_score = Decimal("0")
    feedback_source = None

    def score_for(item: Submission) -> Decimal:
        resolved_score, _ = resolve_submission_score(item)
        return resolved_score if resolved_score is not None else Decimal("0")

    snapshot = db.scalar(
        select(FinalGradeSnapshot).where(
            FinalGradeSnapshot.student_id == submission.user_id,
            FinalGradeSnapshot.question_id == submission.question_id,
        )
    )
    use_historical_highest = bool(snapshot.use_historical_highest) if snapshot is not None else False

    if not submissions:
        effective = None
        effective_score = Decimal("0")
        feedback_source = None
        if snapshot is None:
            return
    elif use_historical_highest:
        for item in submissions:
            current_score = score_for(item)
            if effective is None or current_score > effective_score or (
                current_score == effective_score and item.submitted_at >= effective.submitted_at
            ):
                effective = item
                effective_score = current_score
        if effective is not None:
            _, feedback_source = resolve_submission_score(effective)
    else:
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
    snapshot.question_version_id = effective.question_version_id if effective else None
    snapshot.updated_at = utcnow()
    db.commit()


def get_submission_artifact_path(submission: Submission, artifact_name: str) -> Path:
    result = submission.evaluation_results[-1] if submission.evaluation_results else None
    if result is None:
        raise FileNotFoundError("Submission artifacts are not available yet.")

    mapping = {
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
