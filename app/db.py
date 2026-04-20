from collections.abc import AsyncGenerator
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


async def get_db() -> AsyncGenerator[Session, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _patch_sqlite_schema(conn) -> None:
    """Lightweight additive migrations for existing SQLite files (no Alembic in v0)."""
    insp = inspect(conn)
    if "platform_llm_token_policy" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("platform_llm_token_policy")}
        if "discussion_posts_page_size" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE platform_llm_token_policy "
                    "ADD COLUMN discussion_posts_page_size INTEGER NOT NULL DEFAULT 50"
                )
            )
    if "discussion_posts" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("discussion_posts")}
        if "deleted_at" not in cols:
            conn.execute(text("ALTER TABLE discussion_posts ADD COLUMN deleted_at DATETIME"))
        if "deleted_by_id" not in cols:
            conn.execute(text("ALTER TABLE discussion_posts ADD COLUMN deleted_by_id INTEGER"))
    if "llm_configs" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("llm_configs")}
        if "description" not in cols:
            conn.execute(text("ALTER TABLE llm_configs ADD COLUMN description TEXT"))
    if "users" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("users")}
        if "storage_quota_override_bytes" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN storage_quota_override_bytes INTEGER"))
    if "platform_llm_token_policy" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("platform_llm_token_policy")}
        if "default_student_storage_bytes" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE platform_llm_token_policy "
                    "ADD COLUMN default_student_storage_bytes INTEGER NOT NULL DEFAULT "
                    + str(100 * 1024 * 1024)
                )
            )
        if "default_teacher_storage_bytes" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE platform_llm_token_policy "
                    "ADD COLUMN default_teacher_storage_bytes INTEGER NOT NULL DEFAULT "
                    + str(1024 * 1024 * 1024)
                )
            )
    if "submissions" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("submissions")}
        if "stored_file_purged_at" not in cols:
            conn.execute(text("ALTER TABLE submissions ADD COLUMN stored_file_purged_at DATETIME"))
        if "stored_file_purge_actor" not in cols:
            conn.execute(text("ALTER TABLE submissions ADD COLUMN stored_file_purge_actor VARCHAR(32)"))
    if "courses" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("courses")}
        if "is_hidden_from_course_lists" not in cols:
            conn.execute(text("ALTER TABLE courses ADD COLUMN is_hidden_from_course_lists BOOLEAN NOT NULL DEFAULT 0"))
    if "discussion_topics" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("discussion_topics")}
        if "free_discussion_topic_id" not in cols:
            conn.execute(text("ALTER TABLE discussion_topics ADD COLUMN free_discussion_topic_id INTEGER"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_discussion_topics_free_discussion_topic_id "
                    "ON discussion_topics (free_discussion_topic_id)"
                )
            )
        if "discussion_group_id" not in cols:
            conn.execute(text("ALTER TABLE discussion_topics ADD COLUMN discussion_group_id INTEGER"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_discussion_topics_discussion_group_id "
                    "ON discussion_topics (discussion_group_id)"
                )
            )
    if "discussion_posts" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("discussion_posts")}
        if "visibility_snapshot" not in cols:
            conn.execute(
                text("ALTER TABLE discussion_posts ADD COLUMN visibility_snapshot VARCHAR(16) NOT NULL DEFAULT 'public'")
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_discussion_posts_visibility_snapshot "
                    "ON discussion_posts (visibility_snapshot)"
                )
            )
    if "course_materials" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("course_materials")}
        if "free_discussion_topic_id" not in cols:
            conn.execute(text("ALTER TABLE course_materials ADD COLUMN free_discussion_topic_id INTEGER"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_course_materials_free_discussion_topic_id "
                    "ON course_materials (free_discussion_topic_id)"
                )
            )


def init_database() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as conn:
            _patch_sqlite_schema(conn)
    _ensure_open_community_course()
    _backfill_user_storage()
    _repair_existing_state()


def _backfill_user_storage() -> None:
    from app.services.user_storage import backfill_stored_objects_from_disk

    with SessionLocal() as db:
        try:
            backfill_stored_objects_from_disk(db)
            db.commit()
        except Exception:
            db.rollback()


def _repair_existing_state() -> None:
    from app.services.maintenance import repair_existing_state

    with SessionLocal() as db:
        try:
            repair_existing_state(db)
            db.commit()
        except Exception:
            db.rollback()


def ensure_data_directories() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)


def _ensure_open_community_course() -> None:
    """Single platform-wide course: all active users are members as students; no designated teacher."""
    from app.constants import CourseRole, CourseStatus, MembershipStatus
    from app.models import Course, CourseMember, User

    code = "__OPEN_COMMUNITY__"
    with SessionLocal() as db:
        course = db.scalar(select(Course).where(Course.code == code))
        if course is None:
            course = Course(
                code=code,
                join_code=None,
                title="自由讨论区",
                description="平台公共讨论区（后台载体）：在导航栏进入「自由讨论区」参与话题；不在课程列表中显示。",
                status=CourseStatus.ACTIVE,
                is_open_community=True,
                is_hidden_from_course_lists=True,
                created_by=None,
            )
            db.add(course)
            db.commit()
            db.refresh(course)
        else:
            course.is_hidden_from_course_lists = True
            if not course.is_open_community:
                course.is_open_community = True
            if not (course.title or "").strip():
                course.title = "自由讨论区"
            db.commit()

        user_ids = list(db.scalars(select(User.id).where(User.is_active.is_(True))).all())
        for uid in user_ids:
            row = db.scalar(
                select(CourseMember).where(CourseMember.course_id == course.id, CourseMember.user_id == uid)
            )
            if row is None:
                db.add(
                    CourseMember(
                        course_id=course.id,
                        user_id=uid,
                        role=CourseRole.STUDENT,
                        status=MembershipStatus.ACTIVE,
                    )
                )
            else:
                row.status = MembershipStatus.ACTIVE
                row.role = CourseRole.STUDENT
        db.commit()
