from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import get_current_user, landing_path_for_user, push_flash
from app.db import get_db
from app.i18n import choose_text


router = APIRouter()

RETIRED_NOTEBOOK_MESSAGE_EN = (
    "The standalone Notebook execution workflow has been retired. "
    "Use code questions for executable Python, C, or C++ submissions, or submit .ipynb "
    "through the file / LLM-reviewed workflow."
)
RETIRED_NOTEBOOK_MESSAGE_ZH = (
    "独立的 Notebook 执行流程已下线。"
    "可执行的编程题请使用代码题提交 Python、C 或 C++；"
    "需要提交 .ipynb 时，请使用文件 / LLM 评测流程。"
)


def _retired_redirect(request: Request, db: Session) -> RedirectResponse:
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    push_flash(
        request,
        choose_text(request, RETIRED_NOTEBOOK_MESSAGE_EN, RETIRED_NOTEBOOK_MESSAGE_ZH),
        "info",
    )
    return RedirectResponse(url="/student/help/python-runtime", status_code=303)


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return RedirectResponse(url=landing_path_for_user(user), status_code=303)


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    return _retired_redirect(request, db)


@router.get("/jobs/new")
def new_job_page(request: Request, db: Session = Depends(get_db)):
    return _retired_redirect(request, db)


@router.post("/jobs")
async def create_job(request: Request, db: Session = Depends(get_db)):
    return _retired_redirect(request, db)


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    return _retired_redirect(request, db)


@router.get("/jobs/{job_id}/artifacts/{artifact_name}")
def download_artifact(artifact_name: str, job_id: int, request: Request, db: Session = Depends(get_db)):
    return _retired_redirect(request, db)
