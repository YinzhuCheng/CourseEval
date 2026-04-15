from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import get_current_user, is_admin, is_super_admin, is_teacher_account, push_flash
from app.constants import CourseRole, MembershipStatus
from app.i18n import t
from app.models import Course, CourseMember, User


class RedirectRequired(Exception):
    def __init__(self, location: str):
        self.location = location


COURSE_STAFF_ROLES = {CourseRole.TEACHER, CourseRole.TA}
COURSE_MANAGEMENT_ROLES = {CourseRole.TEACHER}


def is_platform_admin(user: User | None) -> bool:
    return is_admin(user)


def require_super_admin(request: Request, db: Session) -> User:
    user = require_user(request, db)
    if not is_super_admin(user):
        push_flash(request, t(request, "flash.super_admin_required"), "danger")
        raise RedirectRequired("/dashboard")
    return user


def get_course_membership(db: Session, course_id: int, user_id: int) -> CourseMember | None:
    statement = select(CourseMember).where(
        CourseMember.course_id == course_id,
        CourseMember.user_id == user_id,
        CourseMember.status == MembershipStatus.ACTIVE,
    )
    return db.scalar(statement)


def get_course_role(db: Session, course_id: int, user_id: int) -> CourseRole | None:
    membership = get_course_membership(db, course_id, user_id)
    return membership.role if membership else None


def can_view_course(db: Session, course: Course, user: User | None) -> bool:
    if user is None or not user.is_active:
        return False
    if is_platform_admin(user):
        return True
    return get_course_membership(db, course.id, user.id) is not None


def can_manage_course(db: Session, course: Course, user: User | None) -> bool:
    if user is None or not user.is_active:
        return False
    if is_platform_admin(user):
        return True
    role = get_course_role(db, course.id, user.id)
    return role in COURSE_MANAGEMENT_ROLES


def can_staff_course(db: Session, course: Course, user: User | None) -> bool:
    if user is None or not user.is_active:
        return False
    if is_platform_admin(user):
        return True
    role = get_course_role(db, course.id, user.id)
    return role in COURSE_STAFF_ROLES


def require_user(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if user is None or not user.is_active:
        push_flash(request, t(request, "flash.auth_required"), "warning")
        raise RedirectRequired("/login")
    return user


def require_login(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if user is None or not user.is_active:
        raise PermissionError("Authentication required.")
    return user


def require_admin(request: Request, db: Session) -> User:
    user = require_user(request, db)
    if not is_platform_admin(user):
        push_flash(request, t(request, "flash.admin_required"), "danger")
        raise RedirectRequired("/dashboard")
    return user


def require_teacher_account(request: Request, db: Session) -> User:
    user = require_user(request, db)
    if not is_teacher_account(user):
        push_flash(request, t(request, "flash.teacher_account_required"), "danger")
        raise RedirectRequired("/student/courses")
    return user


def require_course_role(db: Session, user_id: int, course_id: int, allowed_roles: set[CourseRole]) -> CourseMember:
    membership = get_course_membership(db, course_id, user_id)
    if membership is None or membership.role not in allowed_roles:
        raise PermissionError("Course role requirement not met.")
    return membership


def require_student_access(db: Session, user_id: int, course_id: int) -> CourseMember:
    return require_course_role(db, user_id, course_id, {CourseRole.STUDENT, *COURSE_STAFF_ROLES})
