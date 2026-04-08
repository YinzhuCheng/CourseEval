from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import get_current_user, push_flash
from app.config import get_settings
from app.constants import JobStatus
from app.db import get_db
from app.models import Job
from app.services.jobs import (
    create_job_with_upload,
    enqueue_notebook_job,
    get_artifact_path,
    get_job_for_user,
    list_jobs_for_user,
    read_text_artifact,
)
from app.web import render_template


router = APIRouter()
settings = get_settings()


def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if user is None:
        push_flash(request, "Please sign in to continue.", "warning")
        raise RedirectRequired("/login")
    return user


class RedirectRequired(Exception):
    def __init__(self, location: str):
        self.location = location


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    destination = "/dashboard" if user else "/login"
    return RedirectResponse(url=destination, status_code=303)


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    jobs = list_jobs_for_user(db, user.id)
    has_active_jobs = any(job.status in {JobStatus.QUEUED, JobStatus.RUNNING} for job in jobs)
    return render_template(request, db, "dashboard.html", {"jobs": jobs, "has_active_jobs": has_active_jobs})


@router.get("/jobs/new")
def new_job_page(request: Request, db: Session = Depends(get_db)):
    try:
        require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)
    return render_template(
        request,
        db,
        "new_job.html",
        {"upload_limit_mb": settings.upload_max_bytes // (1024 * 1024)},
    )


@router.post("/jobs")
async def create_job(
    request: Request,
    notebook_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    filename = notebook_file.filename or ""
    if not filename.lower().endswith(".ipynb"):
        push_flash(request, "Only .ipynb files are accepted.", "danger")
        return RedirectResponse(url="/jobs/new", status_code=303)

    file_bytes = await notebook_file.read(settings.upload_max_bytes + 1)
    if len(file_bytes) > settings.upload_max_bytes:
        push_flash(
            request,
            f"File is too large. Maximum allowed size is {settings.upload_max_bytes // (1024 * 1024)} MB.",
            "danger",
        )
        return RedirectResponse(url="/jobs/new", status_code=303)

    try:
        job = create_job_with_upload(
            db,
            user_id=user.id,
            original_filename=Path(filename).name,
            notebook_bytes=file_bytes,
        )
        enqueue_notebook_job(job.id)
        push_flash(request, f"Notebook uploaded. Job #{job.id} is queued.", "success")
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    except Exception as exc:
        failed_job = db.get(Job, getattr(job, "id", None)) if "job" in locals() else None
        if failed_job is not None:
            failed_job.status = JobStatus.FAILED
            failed_job.error_message = f"Queue submission failed: {exc}"
            failed_job.exit_code = -1
            db.commit()
        push_flash(request, f"Failed to create job: {exc}", "danger")
        return RedirectResponse(url="/jobs/new", status_code=303)


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    job = get_job_for_user(db, job_id, user.id)
    if job is None:
        push_flash(request, "Job not found.", "danger")
        return RedirectResponse(url="/dashboard", status_code=303)

    return render_template(
        request,
        db,
        "job_detail.html",
        {
            "job": job,
            "stdout_text": read_text_artifact(job, "stdout"),
            "stderr_text": read_text_artifact(job, "stderr"),
        },
    )


@router.get("/jobs/{job_id}/artifacts/{artifact_name}")
def download_artifact(
    artifact_name: str,
    job_id: int,
    request: Request,
    download: int = Query(1),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)

    job = get_job_for_user(db, job_id, user.id)
    if job is None:
        push_flash(request, "Job not found.", "danger")
        return RedirectResponse(url="/dashboard", status_code=303)

    try:
        artifact_path = get_artifact_path(job, artifact_name)
    except FileNotFoundError:
        push_flash(request, "Requested artifact is not available yet.", "warning")
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)

    media_type = None
    filename = artifact_path.name
    if artifact_name == "html":
        media_type = "text/html"
        filename = f"job-{job.id}.html"
    elif artifact_name == "executed_notebook":
        media_type = "application/x-ipynb+json"
        filename = f"job-{job.id}-executed.ipynb"
    elif artifact_name in {"stdout", "stderr"}:
        media_type = "text/plain"
        filename = f"job-{job.id}-{artifact_name}.txt"

    response = FileResponse(
        path=artifact_path,
        media_type=media_type,
        filename=filename,
    )
    if artifact_name == "html" and download == 0:
        response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return response
