import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import fitz
import nbformat
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
    LLMScope,
    LLMTestStatus,
    MembershipStatus,
    QuestionType,
    ScoringRule,
    SubmissionLimitMode,
    SubmissionStatus,
)
from app.db import SessionLocal, utcnow
from app.services.llm import (
    ImageInput,
    generate_notebook_evaluation_with_llm,
    generate_file_evaluation_from_images,
    generate_short_answer_evaluation,
)
from app.services.llm_retry import retry_llm_grading_call
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
    LLMConfig,
    NotebookQuestionConfig,
    PythonCodeQuestionConfig,
    Question,
    QuestionVersion,
    RuntimeImage,
    Submission,
)


logger = logging.getLogger(__name__)
settings = get_settings()
PYTHON_EVALUATION_QUEUE = "python-evaluation"
LLM_EVALUATION_QUEUE_PREFIX = "llm-evaluation"


@dataclass
class RunnerResult:
    exit_code: int
    error_message: str | None = None
    summary_json: dict | None = None


def _latest_platform_llm_config(db: Session) -> LLMConfig | None:
    statement = (
        select(LLMConfig)
        .where(
            LLMConfig.scope == LLMScope.PLATFORM,
            LLMConfig.enabled.is_(True),
            LLMConfig.last_test_status == LLMTestStatus.SUCCESS,
        )
        .order_by(LLMConfig.last_tested_at.desc(), LLMConfig.created_at.desc())
    )
    return db.scalar(statement)


def _resolve_llm_config_for_question(question: Question, db: Session | None = None) -> LLMConfig | None:
    question_level = question.llm_config
    if question_level is not None and question_level.enabled:
        return question_level

    assignment_level = question.assignment.llm_config
    if assignment_level is not None and assignment_level.enabled:
        return assignment_level

    course = question.assignment.course
    if not course.use_global_llm_default:
        course_level = course.default_llm_config
        if course_level is not None and course_level.enabled:
            return course_level

    if db is None:
        return None
    return _latest_platform_llm_config(db)


def _clamp_score(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(value, upper))


_HIDDEN_STDOUT_MARKER = "=== Hidden Tests ==="
_HIDDEN_STDERR_MARKER = "=== Hidden Test stderr ==="
_RETIRED_NOTEBOOK_MESSAGE = (
    "Notebook execution has been retired. Use native Python code questions for .py submissions, "
    "or use file / LLM-reviewed questions for .ipynb submissions."
)


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
    """True only when the question explicitly opts into \"teacher must confirm before any score\".

    Default product behavior: LLM scores (when present) are effective without teacher confirmation.
    This flag is then an optional strict gate for rare courses that want no score until a teacher posts.
    """
    question = submission.question
    if submission.submission_type == QuestionType.SHORT_ANSWER:
        config = question.short_answer_config if question is not None else None
        return bool(config and config.teacher_confirmation_required)
    if submission.submission_type in {QuestionType.PDF_LLM, QuestionType.FORMATTED_TEXT_LLM, QuestionType.FILE_LLM}:
        config = _file_question_config(question)
        return bool(config and config.teacher_confirmation_required)
    return False


def _submission_has_llm_score(submission: Submission) -> bool:
    return any(
        item.source == FeedbackSource.LLM and item.score_suggestion is not None for item in submission.feedback_items
    )


def submission_eligible_for_gradebook(submission: Submission) -> bool:
    if submission.counts_toward_limit or submission.is_effective_submission:
        return True
    if submission_requires_teacher_confirmation(submission) and _submission_has_llm_score(submission):
        return True
    return False


def submission_has_teacher_feedback(submission: Submission) -> bool:
    return _latest_feedback(submission, FeedbackSource.TEACHER) is not None


def is_submission_pending_teacher_review(submission: Submission) -> bool:
    """Pending only when the question opted into strict teacher-first grading and no teacher score yet.

    With the default (no strict flag), LLM scores are effective and this is always False.
    """
    if not submission_requires_teacher_confirmation(submission):
        return False
    return not submission_has_teacher_feedback(submission)


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

    llm_feedback = _latest_feedback(submission, FeedbackSource.LLM)
    if llm_feedback is not None and llm_feedback.score_suggestion is not None:
        if submission_requires_teacher_confirmation(submission):
            # Strict opt-in: no displayed/final score from LLM until a teacher posts feedback.
            return None, None
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


def get_queue(queue_name: str) -> Queue:
    return Queue(queue_name, connection=redis_connection())


def get_python_queue_name() -> str:
    return settings.python_queue_name or PYTHON_EVALUATION_QUEUE


def llm_queue_name_for_config(config: LLMConfig) -> str:
    return f"{settings.llm_queue_prefix or LLM_EVALUATION_QUEUE_PREFIX}-{config.id}"


def active_llm_queue_names(db: Session) -> list[str]:
    configs = list(
        db.scalars(
            select(LLMConfig)
            .where(LLMConfig.enabled.is_(True), LLMConfig.queue_concurrency > 0)
            .order_by(LLMConfig.id.asc())
        ).all()
    )
    return [llm_queue_name_for_config(config) for config in configs]


def active_llm_worker_specs(db: Session) -> list[tuple[str, int]]:
    configs = list(
        db.scalars(
            select(LLMConfig)
            .where(LLMConfig.enabled.is_(True), LLMConfig.queue_concurrency > 0)
            .order_by(LLMConfig.id.asc())
        ).all()
    )
    return [(llm_queue_name_for_config(config), max(int(config.queue_concurrency or 1), 1)) for config in configs]


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
            extracted = _render_notebook_as_text(path, require_outputs=False)
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
    if submission.notebook:
        try:
            return _render_notebook_as_text(
                absolute_data_path(submission.notebook.stored_path),
                require_outputs=False,
            )
        except Exception:
            return ""
    return ""


def _find_previous_submission_with_feedback(db: Session, submission: Submission) -> Submission | None:
    statement = (
        select(Submission)
        .options(
            joinedload(Submission.feedback_items),
            joinedload(Submission.notebook),
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
    rq_job = get_queue(get_python_queue_name()).enqueue(
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
    due_at = _as_utc(question.assignment.due_at)
    if due_at is None:
        return False
    return now > due_at


def _submission_window_open(question: Question) -> tuple[bool, str | None]:
    now = _now()
    assignment = question.assignment
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

    ncfg = question.notebook_config
    if ncfg is None:
        raise ValueError("Notebook question configuration is missing.")

    _ensure_text_file_extension(original_filename, {".ipynb"})
    upload_dir = settings.uploads_dir / f"user-{user_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = upload_dir / f"{uuid4().hex}.ipynb"
    stored_path.write_bytes(notebook_bytes)
    relative_nb = relative_to_data(stored_path)

    notebook = Notebook(
        user_id=user_id,
        original_filename=original_filename,
        stored_path=relative_nb,
    )
    db.add(notebook)
    db.flush()

    extracted = _render_notebook_as_text(stored_path, require_outputs=False)
    llm_enabled = ncfg.llm_feedback_enabled and _resolve_llm_config_for_question(question, db) is not None
    submission = Submission(
        course_id=question.assignment.course_id,
        assignment_id=question.assignment_id,
        question_id=question.id,
        user_id=user_id,
        submission_type=QuestionType.NOTEBOOK,
        status=SubmissionStatus.SUBMITTED if llm_enabled else SubmissionStatus.COMPLETED,
        original_filename=original_filename,
        notebook_id=notebook.id,
        answer_text=extracted,
        submitted_at=utcnow(),
        completed_at=utcnow() if not llm_enabled else None,
        is_late=_is_late(question),
        counts_toward_limit=True,
        is_effective_submission=not llm_enabled,
        question_version_id=question.current_question_version_id,
    )
    db.add(submission)
    db.flush()
    if llm_enabled:
        db.add(
            EvaluationTask(
                submission_id=submission.id,
                task_type=EvaluationTaskType.NOTEBOOK_LLM_FEEDBACK,
                backend_type="rq",
                status=EvaluationTaskStatus.QUEUED,
            )
        )
        db.commit()
        db.refresh(submission)
        enqueue_notebook_llm_feedback(db, submission.id)
    else:
        db.commit()
        db.refresh(submission)
    if submission.is_effective_submission:
        update_final_grade_snapshot(db, submission)
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
        question_version_id=question.current_question_version_id,
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
    pdf_page_paths: list[Path] = []
    use_pdf_pipeline = extension == ".pdf" and question.question_type in {
        QuestionType.PDF_LLM,
        QuestionType.FILE_LLM,
    }
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
        question_version_id=question.current_question_version_id,
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

    rq_job = get_queue(get_python_queue_name()).enqueue(
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

    llm_config = _resolve_llm_config_for_question(submission.question, db)
    if llm_config is None:
        raise ValueError("No enabled LLM config available.")
    rq_job = get_queue(llm_queue_name_for_config(llm_config)).enqueue(
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

    llm_config = _resolve_llm_config_for_question(submission.question, db)
    if llm_config is None:
        raise ValueError("No enabled LLM config available.")
    rq_job = get_queue(llm_queue_name_for_config(llm_config)).enqueue(
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
        raise ValueError("Notebook LLM task not found.")

    llm_config = _resolve_llm_config_for_question(submission.question, db)
    if llm_config is None:
        raise ValueError("No enabled LLM config available.")
    rq_job = get_queue(llm_queue_name_for_config(llm_config)).enqueue(
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
    output_dir = absolute_data_path(output_dir_relative_path)
    stdout_path = output_dir / "stdout.txt"
    stderr_path = output_dir / "stderr.txt"
    summary_path = output_dir / "summary.json"
    ensure_writable_directory(output_dir)

    for artifact_path in (stdout_path, stderr_path, summary_path):
        if artifact_path.exists():
            artifact_path.unlink()

    summary = {
        "failure_type": "system_error",
        "message": _RETIRED_NOTEBOOK_MESSAGE,
        "run_success": False,
        "auto_score": 0,
        "visible_score": 0,
        "hidden_score": 0,
    }
    write_text(stdout_path, "")
    write_text(stderr_path, f"{_RETIRED_NOTEBOOK_MESSAGE}\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
    return RunnerResult(exit_code=1, error_message=_RETIRED_NOTEBOOK_MESSAGE, summary_json=summary)


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

        llm_config = _resolve_llm_config_for_question(submission.question, db)
        if llm_config is None or not llm_config.enabled:
            task.status = EvaluationTaskStatus.FAILED
            task.error_message = "No enabled LLM config available."
            task.finished_at = utcnow()
            db.commit()
            return

        task.status = EvaluationTaskStatus.RUNNING
        task.started_at = utcnow()
        db.commit()

        notices: list[str] = []
        prev = _find_previous_submission_with_feedback(db, submission)
        prev_answer = _submission_text_for_llm_context(prev) if prev else ""
        prev_fb = _latest_feedback_comment(prev) if prev else ""
        prev_score = _latest_teacher_score_text(prev) if prev else ""
        trunc_notice = ""
        answer_text = _truncate_for_llm("Student answer", submission.answer_text or "", 24000, notices)
        if notices:
            trunc_notice = " ".join(notices)

        def _run_sa() -> dict:
            return generate_short_answer_evaluation(
                llm_config,
                question_title=submission.question.title,
                question_description=submission.question.description or "",
                rubric_text=submission.question.short_answer_config.rubric_text
                if submission.question.short_answer_config
                else "",
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

        result = retry_llm_grading_call(llm_config, _run_sa, label="short_answer_llm")
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
        submission.is_effective_submission = True
        submission.failure_reason_code = None
        task.status = EvaluationTaskStatus.SUCCEEDED
        task.finished_at = utcnow()
        db.commit()
        refresh_final_grade_snapshot(db, submission.question_id, submission.user_id)
    except Exception as exc:
        logger.exception("Short-answer LLM evaluation failed for submission %s", submission_id)
        task = db.get(EvaluationTask, task_id)
        if task is not None:
            task.status = EvaluationTaskStatus.FAILED
            task.finished_at = utcnow()
            task.error_message = str(exc)
            db.commit()
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
            )
            .where(Submission.id == submission_id)
        )
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None:
            return

        llm_config = _resolve_llm_config_for_question(submission.question, db)
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

        notices: list[str] = []
        ref_merged = _merged_reference_answer_text(
            base_text=question_config.reference_answer_text,
            file_relative_path=question_config.reference_answer_file_path,
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
        if submission.stored_file_path and (
            submission.submission_type == QuestionType.PDF_LLM
            or (submission.submission_type == QuestionType.FILE_LLM and ext == ".pdf")
        ):
            pdf_path = absolute_data_path(submission.stored_file_path)
            page_dir = pdf_path.parent / f"{pdf_path.stem}-pages"
            page_paths = sorted(page_dir.glob("page-*.png"))
            if not page_paths:
                page_paths = _render_pdf_pages_to_images(pdf_path)

            def _run_pdf() -> dict:
                return generate_file_evaluation_from_images(
                    llm_config,
                    question_title=submission.question.title,
                    question_description=submission.question.description or "",
                    rubric_text=question_config.rubric_text,
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

            result = retry_llm_grading_call(llm_config, _run_pdf, label="file_llm_pdf")
        else:
            ans = _truncate_for_llm("Student answer", submission.answer_text or "", 24000, notices)
            if notices and not trunc_notice:
                trunc_notice = " ".join(notices)

            def _run_text() -> dict:
                return generate_short_answer_evaluation(
                    llm_config,
                    question_title=submission.question.title,
                    question_description=submission.question.description or "",
                    rubric_text=question_config.rubric_text,
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
                )

            result = retry_llm_grading_call(llm_config, _run_text, label="file_llm_text")

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
        raise
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
                joinedload(Submission.notebook),
                joinedload(Submission.evaluation_results),
                joinedload(Submission.feedback_items),
            )
            .where(Submission.id == submission_id)
        )
        task = db.get(EvaluationTask, task_id)
        if submission is None or task is None:
            return

        llm_config = _resolve_llm_config_for_question(submission.question, db)
        ncfg = submission.question.notebook_config if submission.question else None
        if llm_config is None or not llm_config.enabled or ncfg is None:
            task.status = EvaluationTaskStatus.FAILED
            task.error_message = "No enabled LLM config or notebook question config."
            task.finished_at = utcnow()
            db.commit()
            return

        if submission.notebook is None:
            task.status = EvaluationTaskStatus.FAILED
            task.error_message = "Notebook file is missing for this submission."
            task.finished_at = utcnow()
            db.commit()
            return

        task.status = EvaluationTaskStatus.RUNNING
        task.started_at = utcnow()
        db.commit()

        nb_path = absolute_data_path(submission.notebook.stored_path)
        student_text = _render_notebook_as_text(nb_path, require_outputs=False)
        latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
        summary_payload = _parsed_summary_json(latest_result)
        summary_json = json.dumps(summary_payload, ensure_ascii=True, indent=2) if summary_payload else "{}"
        stdout_text = read_submission_artifact_text(latest_result, "stdout", max_chars=8000) if latest_result else ""
        stderr_text = read_submission_artifact_text(latest_result, "stderr", max_chars=8000) if latest_result else ""
        auto_score = float(latest_result.auto_score) if latest_result and latest_result.auto_score is not None else 0.0
        max_llm = float(ncfg.llm_score_weight or 0)

        notices: list[str] = []
        ref_merged = _merged_reference_answer_text(
            base_text=ncfg.reference_answer_text or "",
            file_relative_path=ncfg.reference_answer_file_path,
            notices=notices,
        )
        prev = _find_previous_submission_with_feedback(db, submission)
        prev_answer = _submission_text_for_llm_context(prev) if prev else ""
        prev_answer = _truncate_for_llm("Previous submission", prev_answer, 12000, notices)
        prev_fb = _truncate_for_llm("Previous feedback", _latest_feedback_comment(prev) if prev else "", 8000, notices)
        prev_score = _latest_teacher_score_text(prev) if prev else ""
        student_text = _truncate_for_llm("Student submission", student_text, 24000, notices)
        ref_for_prompt = _truncate_for_llm("Reference answer", ref_merged, 24000, notices)
        summary_json = _truncate_for_llm("Evaluation summary JSON", summary_json, 12000, notices)
        stdout_text = _truncate_for_llm("stdout", stdout_text, 8000, notices)
        stderr_text = _truncate_for_llm("stderr", stderr_text, 8000, notices)
        trunc_notice = " ".join(notices) if notices else ""

        def _run_nb() -> dict:
            return generate_notebook_evaluation_with_llm(
                llm_config,
                question_title=submission.question.title,
                question_description=submission.question.description or "",
                rubric_text=ncfg.llm_scoring_rubric or "",
                reference_answer_text=ref_for_prompt,
                student_submission_text=student_text,
                summary_json=summary_json,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                auto_score=auto_score,
                max_llm_score=max_llm,
                previous_submission_text=prev_answer,
                previous_feedback_text=prev_fb,
                previous_teacher_score_text=prev_score,
                truncation_notice=trunc_notice,
                course_llm_response_language=_course_llm_response_language(submission.question),
                bill_user_id=submission.user_id,
                bill_db=db,
            )

        result = retry_llm_grading_call(llm_config, _run_nb, label="notebook_llm")
        comment = result.get("comment_text") or ""
        if notices:
            comment = (
                comment + "\n\n" + "\n".join(f"[Grading system notice] {item}" for item in notices)
            ).strip()
        db.add(
            Feedback(
                submission_id=submission.id,
                evaluation_result_id=latest_result.id if latest_result else None,
                source=FeedbackSource.LLM,
                score_suggestion=Decimal(str(result.get("score_suggestion", 0))),
                comment_text=comment,
            )
        )
        db.flush()
        if latest_result is not None:
            db.refresh(submission)
            latest_result.final_score = _recompute_notebook_final_score(submission, latest_result)
        submission.status = SubmissionStatus.COMPLETED
        submission.completed_at = utcnow()
        submission.is_effective_submission = True
        submission.failure_reason_code = None
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
