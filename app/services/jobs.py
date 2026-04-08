import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from redis import Redis
from rq import Queue
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.constants import JobStatus
from app.db import SessionLocal, utcnow
from app.models import Job, JobOutput, Notebook


logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class RunnerResult:
    exit_code: int
    error_message: str | None = None


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


def create_job_with_upload(
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


def enqueue_notebook_job(job_id: int) -> str:
    rq_job = get_queue().enqueue(
        process_job,
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


def get_artifact_path(job: Job, artifact_name: str) -> Path:
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


def read_text_artifact(job: Job, artifact_name: str, max_chars: int = 200000) -> str:
    try:
        artifact_path = get_artifact_path(job, artifact_name)
    except FileNotFoundError:
        return ""

    content = artifact_path.read_text(encoding="utf-8", errors="replace")
    if len(content) > max_chars:
        return f"{content[:max_chars]}\n\n... output truncated in web view ..."
    return content


def cleanup_stale_running_jobs() -> int:
    with SessionLocal() as db:
        running_jobs = list(db.scalars(select(Job).where(Job.status == JobStatus.RUNNING)).all())
        if not running_jobs:
            return 0

        finished_at = utcnow()
        for job in running_jobs:
            job.status = JobStatus.FAILED
            job.finished_at = finished_at
            job.exit_code = -1
            job.error_message = "Worker restarted before this notebook job completed."
        db.commit()
        return len(running_jobs)


def process_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        statement = (
            select(Job)
            .options(joinedload(Job.notebook), joinedload(Job.output))
            .where(Job.id == job_id)
        )
        job = db.scalar(statement)
        if job is None or job.output is None or job.notebook is None:
            logger.error("Job %s could not be loaded for execution.", job_id)
            return

        job.status = JobStatus.RUNNING
        job.started_at = utcnow()
        job.finished_at = None
        job.exit_code = None
        job.error_message = None
        db.commit()

        result = run_job_in_docker(job)

        job.exit_code = result.exit_code
        job.finished_at = utcnow()
        if result.exit_code == 0:
            job.status = JobStatus.SUCCESS
            job.error_message = None
        else:
            job.status = JobStatus.FAILED
            job.error_message = result.error_message or "Notebook execution failed."
        db.commit()
    except Exception as exc:  # pragma: no cover - defensive worker recovery
        logger.exception("Unexpected error while processing job %s", job_id)
        failed_job = db.get(Job, job_id)
        if failed_job is not None:
            failed_job.status = JobStatus.FAILED
            failed_job.finished_at = utcnow()
            failed_job.exit_code = -1
            failed_job.error_message = str(exc)
            db.commit()
    finally:
        db.close()


def run_job_in_docker(job: Job) -> RunnerResult:
    if job.output is None or job.notebook is None:
        return RunnerResult(exit_code=1, error_message="Job metadata is incomplete.")

    input_path = absolute_data_path(job.notebook.stored_path)
    executed_path = absolute_data_path(job.output.executed_notebook_path)
    html_path = absolute_data_path(job.output.html_path)
    stdout_path = absolute_data_path(job.output.stdout_path)
    stderr_path = absolute_data_path(job.output.stderr_path)
    output_dir = executed_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    for artifact_path in (executed_path, html_path, stdout_path, stderr_path):
        if artifact_path.exists():
            if artifact_path.is_file():
                artifact_path.unlink()
            else:
                shutil.rmtree(artifact_path)

    container_name = f"notebook-job-{job.id}-{uuid4().hex[:8]}"
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--memory",
        settings.runner_memory_limit,
        "--cpus",
        settings.runner_cpus,
        "--pids-limit",
        "256",
        "-v",
        f"{input_path.resolve().as_posix()}:/job/input.ipynb:ro",
        "-v",
        f"{output_dir.resolve().as_posix()}:/job/output",
        "-w",
        "/job",
    ]
    if settings.docker_network_disabled:
        command.extend(["--network", "none"])

    command.extend(
        [
            settings.runner_image,
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
            "--timeout",
            str(settings.execution_timeout_seconds),
        ]
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.execution_timeout_seconds + 30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _force_remove_container(container_name)
        message = f"Notebook execution timed out after {settings.execution_timeout_seconds} seconds."
        write_text(stderr_path, f"{message}\n")
        return RunnerResult(exit_code=124, error_message=message)
    except FileNotFoundError:
        message = "Docker is not installed or is not available in PATH."
        write_text(stderr_path, f"{message}\n")
        return RunnerResult(exit_code=127, error_message=message)

    if completed.stdout.strip():
        write_text(stdout_path, f"{completed.stdout}\n", append=True)
    if completed.stderr.strip():
        write_text(stderr_path, f"{completed.stderr}\n", append=True)

    if completed.returncode != 0:
        message = "Docker runner exited with a non-zero status."
        return RunnerResult(exit_code=completed.returncode, error_message=message)

    missing_artifacts = [
        name
        for name, path in {
            "executed notebook": executed_path,
            "html export": html_path,
            "stdout": stdout_path,
            "stderr": stderr_path,
        }.items()
        if not path.exists()
    ]
    if missing_artifacts:
        message = f"Runner finished without producing required artifacts: {', '.join(missing_artifacts)}."
        write_text(stderr_path, f"{message}\n", append=True)
        return RunnerResult(exit_code=1, error_message=message)

    return RunnerResult(exit_code=0)


def _force_remove_container(container_name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:  # pragma: no cover - best effort cleanup
        logger.warning("Failed to force-remove timed out container %s", container_name)
