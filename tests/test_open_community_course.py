"""Open community course: flag, ordering, and staff access boundaries."""

import secrets

from sqlalchemy import select

from app.constants import CourseRole, MembershipStatus
from app.db import SessionLocal, init_database
from app.models import Course, CourseMember, User
from app.services.courses import (
    OPEN_COMMUNITY_COURSE_CODE,
    get_course_for_staff,
    get_course_for_teacher,
    list_courses_for_student,
)


def test_open_community_course_exists_and_student_does_not_get_staff_views():
    init_database()
    with SessionLocal() as db:
        oc = db.scalar(select(Course).where(Course.code == OPEN_COMMUNITY_COURSE_CODE))
        assert oc is not None
        assert oc.is_open_community is True

        suffix = secrets.token_hex(4)
        user = User(
            username=f"oc_member_{suffix}",
            email=f"oc_member_{suffix}@example.com",
            password_hash="x" * 60,
            email_verified=True,
        )
        db.add(user)
        db.flush()
        db.add(
            CourseMember(
                course_id=oc.id,
                user_id=user.id,
                role=CourseRole.STUDENT,
                status=MembershipStatus.ACTIVE,
            )
        )
        db.commit()

        assert get_course_for_teacher(db, oc.id, user.id) is None
        assert get_course_for_staff(db, oc.id, user.id) is None

        other = Course(
                code=f"ZZZ_{suffix}",
                join_code=f"JC{suffix[:6].upper()}",
                title="Zebra course",
                is_open_community=False,
            )
        db.add(other)
        db.flush()
        db.add(
            CourseMember(
                course_id=other.id,
                user_id=user.id,
                role=CourseRole.STUDENT,
                status=MembershipStatus.ACTIVE,
            )
        )
        db.commit()

        ordered = list_courses_for_student(db, user.id)
        assert ordered[0].code == OPEN_COMMUNITY_COURSE_CODE
