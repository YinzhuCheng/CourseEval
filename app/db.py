from collections.abc import Generator
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = getattr(dbapi_connection, "cursor", None)
    if cursor is None:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_database() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_legacy_schema()


def ensure_data_directories() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)


def migrate_legacy_schema() -> None:
    inspector = inspect(engine)
    if "users" in inspector.get_table_names():
        _ensure_column("users", "account_role", "VARCHAR(20) NOT NULL DEFAULT 'student'")
        _ensure_column("users", "platform_role", "VARCHAR(20) NOT NULL DEFAULT 'user'")
        _ensure_column("users", "email_verified", "BOOLEAN NOT NULL DEFAULT 0")
        _ensure_column("users", "email_verification_token", "VARCHAR(255)")
        _ensure_column("users", "email_verification_sent_at", "DATETIME")
        _ensure_column("users", "is_active", "BOOLEAN NOT NULL DEFAULT 1")
        _ensure_column("users", "updated_at", "DATETIME")
        _normalize_enum_values(
            "users",
            "account_role",
            {
                "TEACHER": "teacher",
                "STUDENT": "student",
                "ADMINISTRATOR": "student",
                "administrator": "student",
            },
        )
        _normalize_enum_values(
            "users",
            "platform_role",
            {
                "USER": "user",
                "ADMIN": "admin",
                "SUPER_ADMIN": "super_admin",
            },
        )
    if "courses" in inspector.get_table_names():
        _ensure_column("courses", "join_code", "VARCHAR(32)")
        _ensure_column("courses", "use_global_llm_default", "BOOLEAN NOT NULL DEFAULT 1")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE courses "
                    "SET use_global_llm_default = 0 "
                    "WHERE default_llm_config_id IS NOT NULL"
                )
            )
    if "notebook_question_configs" in inspector.get_table_names():
        _ensure_column("notebook_question_configs", "llm_score_weight", "NUMERIC(10,2) NOT NULL DEFAULT 0")
        _ensure_column("notebook_question_configs", "llm_scoring_rubric", "TEXT")
    if "llm_configs" in inspector.get_table_names():
        _ensure_column("llm_configs", "queue_concurrency", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column("llm_configs", "supports_vision", "BOOLEAN NOT NULL DEFAULT 0")
    if "submissions" in inspector.get_table_names():
        _ensure_column("submissions", "stored_file_path", "VARCHAR(512)")

    enum_normalizations = {
        "course_members": {
            "role": {"TEACHER": "teacher", "STUDENT": "student", "TA": "ta"},
            "status": {"ACTIVE": "active", "REMOVED": "removed"},
        },
        "courses": {"status": {"ACTIVE": "active", "ARCHIVED": "archived"}},
        "assignments": {
            "status": {
                "DRAFT": "draft",
                "PUBLISHED": "published",
                "CLOSED": "closed",
                "ARCHIVED": "archived",
            },
            "default_scoring_rule": {"LATEST": "latest", "HIGHEST": "highest"},
            "submission_limit_mode": {"UNLIMITED": "unlimited", "DAILY": "daily", "TOTAL": "total"},
        },
        "questions": {
            "question_type": {
                "NOTEBOOK": "notebook",
                "SHORT_ANSWER": "short_answer",
                "PYTHON_CODE": "python_code",
                "PDF": "pdf_llm",
                "FORMATTED_TEXT": "formatted_text_llm",
            },
            "scoring_rule_override": {"LATEST": "latest", "HIGHEST": "highest"},
        },
        "submissions": {
            "submission_type": {
                "NOTEBOOK": "notebook",
                "SHORT_ANSWER": "short_answer",
                "PYTHON_CODE": "python_code",
                "PDF": "pdf_llm",
                "FORMATTED_TEXT": "formatted_text_llm",
            },
            "status": {
                "SUBMITTED": "submitted",
                "QUEUED": "queued",
                "RUNNING": "running",
                "COMPLETED": "completed",
                "FAILED_SYSTEM": "failed_system",
                "FAILED_ANSWER": "failed_answer",
            },
        },
        "evaluation_tasks": {
            "task_type": {
                "NOTEBOOK_EVALUATION": "notebook_evaluation",
                "SHORT_ANSWER_LLM": "short_answer_llm",
                "NOTEBOOK_LLM_FEEDBACK": "notebook_llm_feedback",
                "PYTHON_CODE_EVALUATION": "python_code_evaluation",
                "FILE_LLM_EVALUATION": "file_llm_evaluation",
            },
            "status": {"QUEUED": "queued", "RUNNING": "running", "SUCCEEDED": "succeeded", "FAILED": "failed"},
        },
        "feedback": {"source": {"AUTO": "auto", "LLM": "llm", "TEACHER": "teacher"}},
        "final_grade_snapshots": {
            "grading_rule_applied": {"LATEST": "latest", "HIGHEST": "highest"},
            "feedback_source": {"AUTO": "auto", "LLM": "llm", "TEACHER": "teacher"},
        },
        "jobs": {"status": {"QUEUED": "queued", "RUNNING": "running", "SUCCESS": "success", "FAILED": "failed"}},
        "runtime_images": {"scope": {"PLATFORM": "platform", "COURSE": "course"}},
        "llm_configs": {
            "scope": {"PLATFORM": "platform", "COURSE": "course"},
            "provider_type": {
                "OPENAI_COMPATIBLE": "openai_compatible",
                "GEMINI": "gemini",
                "CLAUDE": "claude",
            },
            "last_test_status": {"NEVER": "never", "SUCCESS": "success", "FAILED": "failed"},
        },
    }

    table_names = set(inspector.get_table_names())
    for table_name, columns in enum_normalizations.items():
        if table_name not in table_names:
            continue
        for column_name, replacements in columns.items():
            _normalize_enum_values(table_name, column_name, replacements)

    _ensure_question_version_schema()


def _ensure_question_version_schema() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "question_versions" not in tables:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE question_versions (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        question_id INTEGER NOT NULL,
                        version_number INTEGER NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(question_id) REFERENCES questions (id) ON DELETE CASCADE
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX ix_question_versions_question_version "
                    "ON question_versions (question_id, version_number)"
                )
            )
    if "questions" in tables:
        _ensure_column("questions", "current_question_version_id", "INTEGER")
    if "submissions" in tables:
        _ensure_column("submissions", "question_version_id", "INTEGER")
    if "final_grade_snapshots" in tables:
        _ensure_column("final_grade_snapshots", "question_version_id", "INTEGER")
        _ensure_column("final_grade_snapshots", "use_historical_highest", "BOOLEAN NOT NULL DEFAULT 0")

    if "question_versions" in set(inspect(engine).get_table_names()):
        from sqlalchemy.orm import joinedload

        from app.models import Question
        from app.services.question_versions import create_initial_question_version

        with SessionLocal() as db:
            missing = list(
                db.scalars(
                    select(Question)
                    .options(
                        joinedload(Question.python_code_config),
                        joinedload(Question.short_answer_config),
                        joinedload(Question.file_question_config),
                    )
                    .where(Question.current_question_version_id.is_(None))
                ).unique()
            )
            for question in missing:
                create_initial_question_version(db, question)
            if missing:
                db.commit()

    _backfill_unified_file_llm_types()


def _backfill_unified_file_llm_types() -> None:
    """Normalize legacy PDF / formatted-text LLM rows to file_llm for one grading pipeline."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE questions SET question_type = 'file_llm' "
                "WHERE question_type IN ('pdf_llm', 'formatted_text_llm')"
            )
        )
        connection.execute(
            text(
                "UPDATE submissions SET submission_type = 'file_llm' "
                "WHERE submission_type IN ('pdf_llm', 'formatted_text_llm')"
            )
        )


def _ensure_column(table_name: str, column_name: str, definition_sql: str) -> None:
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in columns:
        return

    ddl = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition_sql}"
    with engine.begin() as connection:
        connection.execute(text(ddl))


def _normalize_enum_values(table_name: str, column_name: str, replacements: dict[str, str]) -> None:
    with engine.begin() as connection:
        for old_value, new_value in replacements.items():
            connection.execute(
                text(
                    f"UPDATE {table_name} "
                    f"SET {column_name} = :new_value "
                    f"WHERE {column_name} = :old_value"
                ),
                {"old_value": old_value, "new_value": new_value},
            )
