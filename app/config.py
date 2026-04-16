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
    app_base_url: str
    registration_invite_code: str
    internal_email_domain: str
    database_url: str
    redis_url: str
    rq_queue_name: str
    python_queue_name: str
    llm_queue_prefix: str
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
    default_locale: str
    timezone_name: str
    email_verification_expire_hours: int
    pdf_review_max_pages: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from_address: str
    smtp_from_name: str
    smtp_starttls: bool
    smtp_use_ssl: bool


@lru_cache
def get_settings() -> Settings:
    data_dir = BASE_DIR / os.getenv("DATA_DIR", "data")
    return Settings(
        app_name=os.getenv("APP_NAME", "CourseEval"),
        secret_key=os.getenv("SECRET_KEY", "change-me-in-production"),
        app_base_url=os.getenv("APP_BASE_URL", "").strip(),
        registration_invite_code=os.getenv("REGISTRATION_INVITE_CODE", "").strip(),
        internal_email_domain=os.getenv("INTERNAL_EMAIL_DOMAIN", "invite.local").strip() or "invite.local",
        database_url=os.getenv("DATABASE_URL", f"sqlite:///{(data_dir / 'app.db').as_posix()}"),
        redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
        rq_queue_name=os.getenv("RQ_QUEUE_NAME", "notebook-jobs"),
        python_queue_name=os.getenv("PYTHON_QUEUE_NAME", "python-evaluations"),
        llm_queue_prefix=os.getenv("LLM_QUEUE_PREFIX", "llm-evaluations"),
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
        default_locale=os.getenv("DEFAULT_LOCALE", "en"),
        timezone_name=os.getenv("TIMEZONE_NAME", "Asia/Shanghai"),
        email_verification_expire_hours=max(int(os.getenv("EMAIL_VERIFICATION_EXPIRE_HOURS", "24")), 1),
        pdf_review_max_pages=max(int(os.getenv("PDF_REVIEW_MAX_PAGES", "8")), 1),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from_address=os.getenv("SMTP_FROM_ADDRESS", "").strip(),
        smtp_from_name=os.getenv("SMTP_FROM_NAME", "").strip(),
        smtp_starttls=_bool_env("SMTP_STARTTLS", True),
        smtp_use_ssl=_bool_env("SMTP_USE_SSL", False),
    )
