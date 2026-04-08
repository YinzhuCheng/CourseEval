import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.env import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str
    secret_key: str
    database_url: str
    redis_url: str
    rq_queue_name: str
    upload_max_bytes: int
    execution_timeout_seconds: int
    runner_image: str
    runner_memory_limit: str
    runner_cpus: str
    docker_network_disabled: bool
    base_dir: Path
    data_dir: Path
    uploads_dir: Path
    outputs_dir: Path
    templates_dir: Path
    static_dir: Path
    sample_dir: Path
    debug: bool


@lru_cache
def get_settings() -> Settings:
    data_dir = BASE_DIR / os.getenv("DATA_DIR", "data")
    return Settings(
        app_name=os.getenv("APP_NAME", "Notebook Runner MVP"),
        secret_key=os.getenv("SECRET_KEY", "change-me-in-production"),
        database_url=os.getenv("DATABASE_URL", f"sqlite:///{(data_dir / 'app.db').as_posix()}"),
        redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
        rq_queue_name=os.getenv("RQ_QUEUE_NAME", "notebook-jobs"),
        upload_max_bytes=int(os.getenv("UPLOAD_MAX_BYTES", str(5 * 1024 * 1024))),
        execution_timeout_seconds=int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "300")),
        runner_image=os.getenv("RUNNER_IMAGE", "notebook-runner-mvp:latest"),
        runner_memory_limit=os.getenv("RUNNER_MEMORY_LIMIT", "1g"),
        runner_cpus=os.getenv("RUNNER_CPUS", "1"),
        docker_network_disabled=_bool_env("DOCKER_NETWORK_DISABLED", True),
        base_dir=BASE_DIR,
        data_dir=data_dir,
        uploads_dir=data_dir / "uploads",
        outputs_dir=data_dir / "outputs",
        templates_dir=BASE_DIR / "app" / "templates",
        static_dir=BASE_DIR / "app" / "static",
        sample_dir=BASE_DIR / "samples",
        debug=_bool_env("DEBUG", False),
    )
