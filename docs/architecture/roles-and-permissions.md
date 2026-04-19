# Architecture: roles and permissions

## Purpose

Explain how the app decides **who may view or mutate** courses, assignments, submissions, and admin settings.

There is **no centralized policy engine**—access is enforced by explicit helper calls in route handlers.

## Identity & session

| Mechanism | Location |
|-----------|----------|
| Session cookie → `user_id` | Starlette `SessionMiddleware` in `app/main.py` |
| Load `User`, enforce `email_verified`, `is_active` | `get_current_user` in `app/auth.py` |

If `email_verified` is false, `get_current_user` clears session and returns `None`.

## Platform-level roles

Stored on `User` (`platform_role`, `account_role`—see `app/models.py` + `app/constants.py`).

| Helper | Meaning |
|--------|---------|
| `is_super_admin(user)` | Full platform control; user management, locked roles. |
| `is_admin(user)` | Admin or super-admin. |
| `is_teacher_account(user)` | Teacher **account** or admin-equivalent—used to show teacher navigation and gate teacher routes. |

**Source:** `app/auth.py`.

**Bootstrap nuance:** First registered user may become super-admin—see `resolve_registration_roles` in `app/auth.py`.

## Course-level roles

`CourseMember` links `user_id` + `course_id` + `CourseRole` + `MembershipStatus`.

| Helper | Meaning |
|--------|---------|
| `get_course_role` | Returns `CourseRole` or `None`. |
| `can_view_course` | Student/TA/teacher member **or** platform admin. |
| `can_manage_course` | Teacher on course **or** platform admin; **false** for open community courses (special case). |
| `can_staff_course` | Teacher or TA (or platform admin). |

**Source:** `app/services/permissions.py`.

## Route-level patterns

| Pattern | Typical use |
|---------|-------------|
| `require_user` | Any logged-in user. |
| `require_teacher_account` | Teacher UI entry (`/teacher/...`) before course-specific checks. |
| `require_admin` / `require_super_admin` | Admin pages. |
| `require_student_access` / `get_course_role` | Student vs staff within a course. |

**Mechanics:** Many student routes use `try/except RedirectRequired` to translate permission failures to redirects with flashes.

## Admin capabilities

Router: `app/routes/admin.py` (prefix `/admin`).

Super-admin-only actions (e.g. user role updates) call `require_super_admin`—grep in file.

## i18n for denial messages

Flash messages use `t(request, "flash....")` keys from `app/i18n.py` — changing permission text may require new keys.

## Templates

`app/templates/base.html` shows teacher/admin links based on `can_use_teacher_features` / `can_access_admin_features` computed in `app/web.py` (`render_template`).

Do not show controls in templates that the POST route does not enforce.

## Tests

Grep `permissions.py` imports in `tests/`; much behavior is integration-tested via routes.

## Common misconceptions

- **Teacher account vs course teacher role:** `require_teacher_account` does not prove the user teaches *this* course—always combine with `can_manage_course` / `can_staff_course` where needed.
- **Admin bypass:** platform admins often bypass membership checks—changing `permissions.py` affects all admin-audited flows.

## Source of truth

1. `app/services/permissions.py`
2. `app/auth.py`
3. Route handlers in `app/routes/*.py`

## Coordinated changes

Any new mutating route: **handler** + `permissions.py` (if new shared rule) + **template** that exposes the action + relevant tests.
