# File map (agent-oriented)

Grouped by **responsibility**, not directory listing. Use this to answer: *where is the source of truth?*, *what is glue vs core?*, *which tests matter?*

---

## HTTP layer & app composition

| Role | Files |
|------|--------|
| FastAPI app, middleware, router mounting | `app/main.py` |
| Settings / env | `app/config.py`, `app/env.py` |
| Template rendering helper, `elabel`, flashes | `app/web.py` |
| Session-backed auth helpers | `app/auth.py` |

**Tests:** Mostly indirect via route tests in `tests/` (grep route path or handler name).

---

## Auth / identity / verification

| Role | Files |
|------|--------|
| Password hash, session user, role booleans | `app/auth.py` |
| Register, login, logout, email verify, password reset, profile/avatar | `app/routes/auth.py` |
| Email delivery helpers | `app/services/email.py` |
| User row schema | `app/models.py` (`User`) |

**Source of truth:** `app/auth.py` + `User` columns. Routes should stay thin.

**Tests:** `tests/test_auth_email_verification.py`.

---

## Roles, permissions, course access

| Role | Files |
|------|--------|
| Course/staff/student checks, `RedirectRequired` | `app/services/permissions.py` |
| Platform admin / teacher account checks | `app/auth.py` |
| Membership queries | `permissions.py` + `CourseMember` in `app/models.py` |
| Enums for roles/status | `app/constants.py` |

**Glue:** Every guarded route in `app/routes/*.py` first ~30 lines per handler.

**Tests:** grep `require_teacher_account`, `can_manage_course`, etc.

---

## Courses, memberships, join codes

| Role | Files |
|------|--------|
| Course CRUD, join, listings, aggregates | `app/services/courses.py` |
| Student course UI | `app/routes/student.py` |
| Teacher course UI, members, covers | `app/routes/teacher.py` |
| Models | `Course`, `CourseMember`, related in `app/models.py` |

**Note:** effective score resolution now lives in `app/services/scoring.py`; keep course listing and analytics code on that shared helper.

**Tests:** `tests/test_open_community_course.py`, others touching courses.

---

## Assignments, questions, versioning

| Role | Files |
|------|--------|
| Assignment + question CRUD (large) | `app/routes/teacher.py` |
| Question version snapshots / payloads | `app/services/question_versions.py` |
| ORM: `Assignment`, `Question`, configs | `app/models.py` |
| Rubric visibility rules | `app/services/assignment_visibility.py` |
| Post-deadline reveal bundle | `app/services/post_close_reveal.py` |

**Orchestration:** `teacher.py` coordinates uploads + DB writes; configs are source of truth in `models.py`.

**Tests:** `tests/test_assignment_visibility.py`, `tests/test_phase1_alignment.py`, grep `create_question` / `update_question`.

---

## Student submission intake (HTTP → service)

| Role | Files |
|------|--------|
| Submission endpoints | `app/routes/student.py` |
| Submission creation, tasks, enqueue, processing | `app/services/submissions.py` |

**Source of truth for lifecycle:** `submissions.py` (very large—use grep for specific functions).

**Tests:** `tests/test_code_runner.py`, `tests/test_file_llm_image_inputs.py`, `tests/test_notebook_multimodal.py`, `tests/test_discussions_and_materials.py` (partial overlap).

---

## Evaluation, queue, worker

| Role | Files |
|------|--------|
| RQ queue names, enqueue, job functions | `app/services/submissions.py` (`enqueue_*`, `process_*`) |
| Worker process layout | `worker.py` |

**Invariant:** `EvaluationTask.task_type` selects processor in enqueue paths—grep `EvaluationTaskType`.

---

## Scoring, feedback, gradebook snapshots

| Role | Files |
|------|--------|
| Effective score resolution, teacher gating | `app/services/scoring.py` |
| Gradebook snapshot recompute | `app/services/submissions.py` (`update_final_grade_snapshot`) |
| Teacher grading HTTP | `app/routes/teacher.py` (`grade_submission`, related) |
| Teacher analytics queries | `app/services/teacher_analytics.py` |
| Student submission UI | `app/templates/student_submission_detail.html` |

**Tests:** `tests/test_teacher_analytics.py`, grep `FinalGradeSnapshot`.

---

## LLM: config, calls, retry, quota

| Role | Files |
|------|--------|
| LLM HTTP / prompt orchestration | `app/services/llm.py` |
| Retry policy | `app/services/llm_retry.py` |
| Prompt text assembly | `app/services/llm_grading_prompts.py` |
| Token accounting | `app/services/llm_token_usage.py` |
| Admin CRUD / tests | `app/routes/admin.py` |
| `.ipynb` multimodal extraction / PDF images | `app/services/notebook_multimodal.py`, `app/services/submissions.py` |

**Tests:** `tests/test_llm_retry.py`, `tests/test_llm_token_usage.py`, `tests/test_file_llm_image_inputs.py`.

---

## Runner & sandbox artifacts

| Role | Files |
|------|--------|
| Docker invocation, host bind mounts, summary ingestion | `app/services/submissions.py` (`run_code_in_docker`, artifact readers) |
| In-container test driver | `runner/execute_code.py` |
| Default runner image / package metadata for UI | `app/runtime_support.py` |

**Tests:** `tests/test_code_runner.py`.

---

## Templates / UI / i18n

| Role | Files |
|------|--------|
| Jinja pages | `app/templates/**/*.html` |
| String tables | `app/i18n.py`, `app/enum_labels.py` |
| Static CSS | `app/static/` (if present; also CDN links in `base.html`) |

Changing copy: often **both** `i18n.py` and `enum_labels.py` (for enum-backed labels).

---

## Discussions, materials, file download

| Role | Files |
|------|--------|
| Discussion threads | `app/services/discussions.py`, templates `partials/discussion_board.html` |
| Optional AI reply | `app/services/discussion_ai.py` |
| Learning materials CRUD/view | `app/services/course_materials.py`, `app/routes/course_content.py` |
| Serving files from data dir | `app/routes/uploads.py` |

---

## Database & scripts

| Role | Files |
|------|--------|
| Engine, session factory, v0 bootstrap | `app/db.py` |
| Schema | `app/models.py` |
| One-off maintenance | `scripts/init_db.py`, `scripts/init_super_admin.py` |

**Migration note:** There is no Alembic tree. Startup creates the v0 schema and seeds the open community course; see `docs/deployment-and-upgrades.md` before adding upgrade scripts.

## Operations and review notes

| Need | Document |
|------|----------|
| Known technical debt / compatibility traps | `docs/known-issues.md` |
| Deployment and upgrade checklist | `docs/deployment-and-upgrades.md` |

---

## Tests (how to navigate)

| Area | Starting tests |
|------|----------------|
| Auth / email | `tests/test_auth_email_verification.py` |
| Code run | `tests/test_code_runner.py` |
| LLM | `tests/test_llm_*.py`, `tests/test_file_llm_image_inputs.py` |
| Courses / community | `tests/test_open_community_course.py` |
| Assignment visibility | `tests/test_assignment_visibility.py` |
| Discussions / materials | `tests/test_discussions_and_materials.py` |

Run all: `python3 -m pytest tests/ -q`.
