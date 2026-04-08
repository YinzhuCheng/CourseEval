import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from app.config import get_settings
from app.db import ensure_data_directories, init_database
from app.routes.auth import router as auth_router
from app.routes.jobs import router as jobs_router


settings = get_settings()
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(title=settings.app_name)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
app.include_router(auth_router)
app.include_router(jobs_router)


@app.on_event("startup")
def startup() -> None:
    ensure_data_directories()
    init_database()


@app.get("/healthz")
def healthz():
    return JSONResponse({"status": "ok"})
