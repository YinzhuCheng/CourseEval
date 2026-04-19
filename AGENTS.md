# AGENTS.md — repository map for AI coding agents

This file is the **top-level navigation layer** for autonomous or assisted coding agents working in this repository. It is optimized for **partial retrieval**: read this first, then open only the deeper docs (`docs/file-map.md`, `docs/change-guide.md`, `docs/architecture/*.md`) that match your task.

Humans may prefer `README.md` for onboarding narrative; agents should use this file plus `docs/` before non-trivial edits.

**Uncertainty policy:** If anything here disagrees with the code, **trust the code**. If the code is ambiguous, inspect call sites and add a short note in your change or in docs rather than guessing.

---

## 1. Repository purpose

**CourseEval** is a web application for course assignments where students submit work (code, files, short text, notebooks-as-files) and the system runs **automated evaluation** (code runner / tests) and/or **LLM-assisted grading suggestions**, often with **teacher confirmation** depending on question configuration.

Supported workflows (see `README.md` for user-facing detail):

- Code questions: Python, C, C++ via Docker-isolated runner (`runner/execute_code.py`).
- File / LLM paths: PDF, formatted text, `.ipynb` as file upload with LLM pipelines in `app/services/llm.py` and related services.
- Gradebook snapshots and teacher analytics built on `FinalGradeSnapshot` and submission history.

**Legacy:** Standalone notebook *execution* jobs and `/jobs/*` UI are retired; `app/routes/jobs.py` redirects. Some model/table names still say `notebook` for historical reasons—verify behavior in `app/services/submissions.py` and `app/models.py`.

---

## 2. Tech stack

| Layer | Technology | Primary locations |
|-------|------------|-------------------|
| HTTP API & routing | **FastAPI** | `app/main.py`, `app/routes/*.py` |
| Persistence | **SQLAlchemy** ORM, SQLite (default) | `app/models.py`, `app/db.py` |
| HTML UI | **Jinja2** templates | `app/templates/`, `app/web.py` (`render_template`) |
| Sessions | Starlette **SessionMiddleware** | `app/main.py` |
| Background work | **Redis + RQ** queues | `app/services/submissions.py` (`enqueue_*`, `get_queue`), `worker.py` |
| Code sandbox | **Docker** + runner script | `app/services/submissions.py` (`run_code_in_docker`, `run_job_in_docker`), `runner/execute_code.py` |
| LLM calls | HTTP clients / provider-specific logic | `app/services/llm.py`, `app/services/llm_retry.py`, `app/services/llm_token_usage.py` |
| i18n | Python dict catalogs + template helpers | `app/i18n.py`, `app/enum_labels.py` |

There is **no separate SPA**; “frontend” is templates + Bootstrap CDN in `app/templates/base.html`.

---

## 3. High-level repository map

| Path | Role |
|------|------|
| `app/main.py` | FastAPI app, middleware, router include order, static mount |
| `app/config.py`, `app/env.py` | Settings from environment |
| `app/constants.py` | Enums and string constants used across ORM and services |
| `app/models.py` | SQLAlchemy models (source of truth for schema) |
| `app/db.py` | Engine, session, migrations/bootstrap helpers |
| `app/auth.py` | Password hashing, session user loading, platform/ account role helpers |
| `app/routes/` | HTTP entrypoints (thin: validate, call service, redirect/render) |
| `app/services/` | Core domain logic (submissions, courses, LLM, permissions, …) |
| `app/web.py` | `render_template` and shared template utilities (`elabel`, flashes) |
| `app/i18n.py`, `app/enum_labels.py` | Localized strings for UI |
| `runner/` | Host-side code runner (`execute_code.py`) invoked from Docker |
| `worker.py` | Process manager spawning RQ workers per Python + LLM queues |
| `tests/` | Pytest suite |
| `scripts/` | CLI maintenance (e.g. DB init, super admin) |

---

## 4. Major subsystems (ownership & first files to open)

### Authentication / registration / verification

- **Owns:** User identity, password hashing, session `user_id`, email verification and password reset tokens.
- **Source of truth:** `app/auth.py`, `app/routes/auth.py`, `User` model fields in `app/models.py`.
- **Also check:** `app/services/email.py`, flashes/messages in `app/i18n.py`.
- **Tests:** `tests/test_auth_email_verification.py`.

### Roles and permissions

- **Owns:** Who may access admin, teacher UI, course management, student views; course-level roles.
- **Source of truth:** `app/auth.py` (`is_admin`, `is_teacher_account`, `is_super_admin`, …), `app/services/permissions.py` (`require_*`, `can_*`, `RedirectRequired`), `CourseMember` + enums in `app/constants.py`.
- **Entrypoints:** All `app/routes/*.py` use permission helpers; never assume a route is unguarded without reading the first lines of the handler.
- **Tests:** scattered; permission behavior often implied by route tests—grep `require_`, `can_`.

### Courses and memberships

- **Owns:** Courses, join codes, membership, open community behavior, aggregates for gradebook.
- **Source of truth:** `app/services/courses.py`, `Course`, `CourseMember`, `Assignment` in `app/models.py`.
- **Routes:** `app/routes/student.py` (join, student course pages), `app/routes/teacher.py` (course CRUD, members).

### Assignments and question configuration

- **Owns:** Assignment lifecycle, questions, per-type configs (`CodeQuestionConfig`, `FileQuestionConfig`, …), question versioning.
- **Source of truth:** `app/models.py` (relationships), `app/routes/teacher.py` (large: create/update questions), `app/services/question_versions.py`.
- **Supporting:** `app/services/assignment_visibility.py` (rubric visibility), `app/runtime_support.py` (allowed libraries metadata for UI).

### Student submissions

- **Owns:** Creating submissions, storing files under data dir, linking to questions, enqueueing evaluation.
- **Source of truth:** `app/services/submissions.py` (very large), `app/routes/student.py` (upload endpoints).
- **Models:** `Submission`, `EvaluationTask`, `EvaluationResult`, `Feedback` in `app/models.py`.

### Evaluation tasks / queue flow

- **Owns:** `EvaluationTask` rows, RQ job enqueue, worker entry functions, status transitions on submission + task.
- **Source of truth:** `app/services/submissions.py` — search `enqueue_`, `process_`, `EvaluationTaskType`, `EvaluationTaskStatus`.
- **Worker:** `worker.py` (queue layout), RQ registers job functions from `submissions` module.

### Scoring / feedback / gradebook snapshots

- **Owns:** `Feedback` rows, `EvaluationResult` scores, **effective** score resolution, `FinalGradeSnapshot` updates for analytics and reveal.
- **Source of truth:** `app/services/submissions.py` — `resolve_submission_score`, `update_final_grade_snapshot`, `submission_requires_teacher_confirmation`, teacher grading in `app/routes/teacher.py`.
- **Caution:** `app/services/courses.py` defines a **different** `resolve_submission_score` with a distinct signature—used for analytics/list paths, not the main submission pipeline (see `docs/architecture/scoring-pipeline.md`).

### LLM config / usage / quota

- **Owns:** `LLMConfig` records, per-user token limits, billing/usage aggregation (Beijing day), LLM queue names.
- **Source of truth:** `app/models.py` (`LLMConfig`, usage tables), `app/services/llm_token_usage.py`, `app/services/llm.py`, admin routes in `app/routes/admin.py`, student usage in `app/routes/student.py`.
- **Tests:** `tests/test_llm_token_usage.py`, `tests/test_llm_retry.py`, etc.

### Runner / sandbox / artifacts

- **Owns:** Docker invocation, paths to stdout/stderr/summary JSON, stripping hidden sections for students.
- **Source of truth:** `app/services/submissions.py` (`run_code_in_docker`, artifact readers), `runner/execute_code.py` (test orchestration and JSON summary shape).
- **Tests:** `tests/test_code_runner.py`.

### Templates / UI rendering

- **Owns:** HTML pages, flashes, i18n keys via `t()` / `lx()` / `elabel()`.
- **Source of truth:** `app/templates/`, strings in `app/i18n.py` and `app/enum_labels.py`.
- **Glue:** `app/web.py` builds template context; changing a template often requires checking the route that renders it and any enum labels.

### Discussion, materials, uploads

- **Owns:** Discussion threads, optional AI reply, course materials, serving user-uploaded files from data directory.
- **Source of truth:** `app/services/discussions.py`, `app/services/discussion_ai.py`, `app/services/course_materials.py`, `app/routes/course_content.py`, `app/routes/uploads.py`.

---

## 5. Key workflows (scannable)

### Submission lifecycle (student → persisted → queued)

1. Route in `app/routes/student.py` receives upload or text.
2. Service in `app/services/submissions.py` creates `Submission`, optional `EvaluationTask`, writes files under configured data paths.
3. `enqueue_*` pushes RQ job; submission status often becomes `queued` / `running` later from worker.

### Evaluation lifecycle (worker)

1. RQ runs `process_code_evaluation`, `process_submission_evaluation` (legacy notebook path), or LLM processors (`process_*_llm_evaluation`).
2. They mutate `Submission`, `EvaluationTask`, create `EvaluationResult` / `Feedback`, then `commit`.
3. **Artifacts:** `EvaluationResult` stores relative paths to stdout/stderr/summary; templates load via services.

### Scoring / feedback lifecycle

1. Automatic: `Feedback` source `auto` from code run summary.
2. LLM: `Feedback` source `llm` from LLM services; may be gated by `teacher_confirmation_required` on question config (see `submission_requires_teacher_confirmation` in `submissions.py`).
3. Teacher: `Feedback` source `teacher` from `app/routes/teacher.py` grading endpoint.
4. `update_final_grade_snapshot` recomputes per-student-per-question snapshot for gradebook and reveal.

### Permission-check flow

1. `get_current_user` in `app/auth.py` (session + email verified).
2. Route calls `require_user`, `require_teacher_account`, `require_admin`, or course-specific checks in `app/services/permissions.py`.
3. On failure, `RedirectRequired` or flash + redirect—**UI flashes are not the authority**; behavior is in code.

### Runner output → user-visible page

1. Runner writes `summary.json`, stdout/stderr under per-submission output dir.
2. `EvaluationResult` rows store relative paths.
3. `build_student_result_view` and template `student_submission_detail.html` read sanitized artifact text via `read_student_safe_submission_artifact_text`.

---

## 6. Files that usually need coordinated changes

| If you change… | Also inspect… |
|-----------------|---------------|
| Submission status semantics | `app/services/submissions.py` (all `SubmissionStatus` transitions), `app/constants.py`, student/teacher templates showing status, `app/enum_labels.py` |
| Question create/edit payload | `app/routes/teacher.py`, `app/models.py` config tables, `app/services/question_versions.py`, templates for that question type |
| Code evaluation / runner JSON | `runner/execute_code.py`, `run_code_in_docker` / `process_code_evaluation`, tests `tests/test_code_runner.py`, student result template |
| LLM grading or token quota | `app/services/llm.py`, `llm_retry.py`, `llm_token_usage.py`, `app/routes/admin.py`, `enqueue_*` in `submissions.py` |
| Permission / role gate | `app/services/permissions.py`, `app/auth.py`, every route handler that should enforce the same rule |
| i18n or status label | `app/i18n.py`, `app/enum_labels.py`, and the template or flash site that displays the string |
| Final gradebook / reveal | `update_final_grade_snapshot` in `submissions.py`, `app/services/post_close_reveal.py`, `app/services/teacher_analytics.py`, teacher routes querying `FinalGradeSnapshot` |

---

## 7. Common traps (do-not-assume)

- **Do not** infer behavior from file names alone (e.g. `notebook` types vs retired execution pipeline).
- **Do not** assume a FastAPI route owns all business rules—large pieces live in `app/services/submissions.py`.
- **Do not** change a template’s variables without checking the route in `app/routes/*` and `app/web.py` context.
- **Do not** assume renaming an enum value in `constants.py` is safe without DB migration / existing row values.
- **Do not** assume `Submission.status` and `EvaluationTask.status` stay in sync automatically—verify both when changing worker code.
- **Two different `resolve_submission_score` functions** exist (`submissions.py` vs `courses.py`)—different call sites; changing one may not fix the other.
- **Branch names** are not architecture documentation.

---

## 8. Validation commands

From repository root (virtualenv or `pip install -r requirements.txt` as needed):

```bash
python3 -m compileall app runner -q
python3 -m pytest tests/ -q
```

There is **no dedicated lint script** in-repo; rely on tests and compile checks unless the host environment adds ruff/mypy.

---

## 9. Deeper documentation index

| Document | Use when… |
|----------|-----------|
| `docs/file-map.md` | You need “which file owns X?” grouped by responsibility |
| `docs/change-guide.md` | You are modifying behavior and need coupling / failure-mode hints |
| `docs/known-issues.md` | You are reviewing technical debt, historical compatibility traps, or doc/code inconsistencies |
| `docs/deployment-and-upgrades.md` | You need deployment, persistence, queue, or migration/upgrade expectations |
| `docs/architecture/submission-lifecycle.md` | Submission + task + worker state flow |
| `docs/architecture/scoring-pipeline.md` | Scores, feedback, snapshots, teacher confirmation |
| `docs/architecture/roles-and-permissions.md` | Access control |
| `docs/architecture/runner-and-sandbox.md` | Docker runner, artifacts, summary JSON |

---

## 10. When stuck

1. Grep for the model or enum (`Submission`, `EvaluationTaskType`, …).
2. Open the **service** file that grep shows for business logic (`app/services/submissions.py` first for eval flows).
3. Open the **route** that calls the service.
4. Run targeted tests (`tests/test_code_runner.py`, etc.).
5. If behavior remains unclear, document **uncertainty** in your PR and prefer minimal, well-tested changes.
