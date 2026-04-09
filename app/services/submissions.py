import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

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
from app.models import (
    Assignment,
    CourseMember,
    EvaluationResult,
    EvaluationTask,
    Feedback,
    FinalGradeSnapshot,
    Job,
    JobOutput,
    Notebook,
    Question,
    Submission,
)


logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class RunnerResult:
    exit_code: int
    error_message: str | None = None
    summary_json: dict | None = None


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


def write_text(path: Path, content: str, append: bool = False) -> None:
    ensure_parent_dir(path)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as file_handle:
        file_handle.write(content)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        status=SubmissionStatus.COMPLETED,
        answer_text=cleaned,
        submitted_at=utcnow(),
        completed_at=utcnow(),
        is_late=_is_late(question),
        counts_toward_limit=True,
        is_effective_submission=True,
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
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

    rq_job = get_queue().enqueue(
        process_submission_evaluation,
        submission_id,
        task.id,
        job_timeout=settings.execution_timeout_seconds + 120,
        result_ttl=86400,
        failure_ttl=86400,
    )
    submission.status = SubmissionStatus.QUEUED
    submission.queued_at = utcnow()
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
        runtime_image_tag = settings.runner_image
        timeout_seconds = notebook_config.time_limit_seconds if notebook_config else settings.execution_timeout_seconds
        memory_limit = (
            f"{notebook_config.memory_limit_mb}m" if notebook_config else settings.runner_memory_limit
        )
        cpus = notebook_config.cpu_limit if notebook_config else settings.runner_cpus
        network_disabled = not (notebook_config.allow_network if notebook_config else False)

        submission.status = SubmissionStatus.RUNNING
        submission.started_at = utcnow()
        task.status = EvaluationTaskStatus.RUNNING
        task.started_at = utcnow()
        task.error_message = None
        db.commit()

        output_dir = settings.outputs_dir / "submissions" / f"submission-{submission.id}"
        output_dir.mkdir(parents=True, exist_ok=True)

        result = run_job_in_docker(
            input_relative_path=submission.notebook.stored_path,
            output_dir_relative_path=relative_to_data(output_dir),
            runner_image=runtime_image_tag,
            timeout_seconds=timeout_seconds,
            memory_limit=memory_limit,
            cpus=cpus,
            network_disabled=network_disabled,
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

        db.commit()
        update_final_grade_snapshot(db, submission)
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


def run_job_in_docker(
    *,
    input_relative_path: str,
    output_dir_relative_path: str,
    runner_image: str,
    timeout_seconds: int,
    memory_limit: str,
    cpus: str,
    network_disabled: bool,
) -> RunnerResult:
    input_path = absolute_data_path(input_relative_path)
    output_dir = absolute_data_path(output_dir_relative_path)
    executed_path = output_dir / "executed.ipynb"
    html_path = output_dir / "executed.html"
    stdout_path = output_dir / "stdout.txt"
    stderr_path = output_dir / "stderr.txt"
    summary_path = output_dir / "summary.json"
    output_dir.mkdir(parents=True, exist_ok=True)

    for artifact_path in (executed_path, html_path, stdout_path, stderr_path, summary_path):
        if artifact_path.exists():
            if artifact_path.is_file():
                artifact_path.unlink()
            else:
                shutil.rmtree(artifact_path)

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
        teacher_scores = [feedback.score_suggestion for feedback in item.feedback_items if feedback.source == FeedbackSource.TEACHER]
        if teacher_scores:
            return Decimal(str(teacher_scores[-1] or 0))
        result = item.evaluation_results[-1] if item.evaluation_results else None
        if result and result.final_score is not None:
            return Decimal(str(result.final_score))
        llm_scores = [feedback.score_suggestion for feedback in item.feedback_items if feedback.source == FeedbackSource.LLM]
        if llm_scores:
            return Decimal(str(llm_scores[-1] or 0))
        return Decimal("0")

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
        teacher_feedback = [feedback for feedback in effective.feedback_items if feedback.source == FeedbackSource.TEACHER]
        llm_feedback = [feedback for feedback in effective.feedback_items if feedback.source == FeedbackSource.LLM]
        auto_feedback = [feedback for feedback in effective.feedback_items if feedback.source == FeedbackSource.AUTO]
        if teacher_feedback:
            feedback_source = FeedbackSource.TEACHER
        elif llm_feedback:
            feedback_source = FeedbackSource.LLM
        elif auto_feedback:
            feedback_source = FeedbackSource.AUTO

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
