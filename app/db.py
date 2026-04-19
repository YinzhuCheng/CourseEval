from collections.abc import Generator
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, select
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
    _ensure_open_community_course()


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
                description="全员公共交流区：可发布学习资料与习题；无固定任课教师，由平台管理员治理。",
                status=CourseStatus.ACTIVE,
                is_open_community=True,
                created_by=None,
            )
            db.add(course)
            db.commit()
            db.refresh(course)
        elif not course.is_open_community:
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
