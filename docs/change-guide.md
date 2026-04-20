# Change guide (coupling & failure modes)

Task-oriented: **if you edit X, you likely must read Y** because of shared invariants, contracts, or duplicated logic.

---

## Submission creation & intake routes

**Touch together:** `app/routes/student.py` (handlers) + `app/services/submissions.py` (`create_*_submission` functions) + `app/models.py` (`Submission`, type-specific configs).

**Why:** Routes validate uploads and delegate; all persisted state and side effects (files on disk, `EvaluationTask` rows, enqueue) live in services.

**Failure mode if incomplete:** HTTP 200 but nothing queued; or DB row without files; or missing `EvaluationTask` so worker has nothing to run.

**Invariant:** Submission `question_version_id` and file paths must stay consistent with teacher edits—grep `question_version_id` when changing versioning.

---

## Queue task state transitions

**Touch together:** `app/services/submissions.py` (`enqueue_submission_evaluation`, `enqueue_*_llm`, `process_*`) + `worker.py` (which queues exist) + `app/models.py` (`EvaluationTask`, `Submission` statuses).

**Why:** `Submission.status` and `EvaluationTask.status` are updated in worker code; enqueue sets RQ metadata and backend job id on the task.

**Failure mode:** Jobs stuck `queued` on one queue while worker listens to another; or double-enqueue if idempotency checks wrong (`_existing_backend_job_id`).

**Invariant:** Grep `EvaluationTaskStatus` and `SubmissionStatus` in `submissions.py` for every transition path.

---

## Scoring logic & gradebook snapshots

**Touch together:** `app/services/scoring.py` — especially `resolve_submission_score`, `submission_requires_teacher_confirmation`, `submission_eligible_for_gradebook` — plus `app/services/submissions.py` (`update_final_grade_snapshot`), **callers** in `app/routes/teacher.py` (grading), templates that show “pending teacher review”, and `app/services/teacher_analytics.py` / `post_close_reveal.py` for snapshot consumers.

**Invariant:** Keep only one `resolve_submission_score` definition. If another module needs score semantics, import from `app/services/scoring.py`.

**Failure mode:** Teacher sees one score in UI while gradebook snapshot or analytics shows another.

---

## Code evaluation & Docker runner

**Touch together:** `runner/execute_code.py` (summary JSON schema, test harness) + `app/services/submissions.py` (`run_code_in_docker`, `process_code_evaluation`, parsing of `summary_json`) + `app/models.py` (`CodeQuestionConfig` tests JSON) + `app/templates/student_submission_detail.html` (displays messages / scores).

**Why:** Changing JSON keys or score fields without updating both runner and consumer breaks silently or throws in templates.

**Failure mode:** `EvaluationResult` stores zeros while student sees empty error; hidden test leakage if `read_student_safe_submission_artifact_text` markers drift.

**Invariant:** Hidden sections in stdout/stderr use markers defined in `submissions.py`—grep `_HIDDEN_STDOUT_MARKER`.

---

## File / short-answer LLM flows

**Touch together:** `app/services/submissions.py` (which task type is created per `QuestionType`, enqueue functions) + `app/services/llm.py` + type-specific helpers (`notebook_multimodal.py`, PDF path in file evaluation) + question config models in `app/models.py`.

**Why:** A question type change without updating enqueue routing means submissions never enter LLM queue.

**Failure mode:** Submission stuck `submitted` with no LLM task; or wrong extractor used for `.ipynb`/PDF.

**Invariant:** `QuestionType.FILE_LLM` is the only file-upload LLM question type. PDF, text, TeX, Markdown, and ipynb behavior is selected by file extension.

---

## Permissions & role gates

**Touch together:** `app/services/permissions.py` + `app/auth.py` + **each** route that should enforce the same rule + templates that show/hide actions.

**Why:** There is no centralized policy framework—checks are explicit function calls.

**Failure mode:** Teacher-only POST endpoint reachable to students if one route skips `require_teacher_account` or course role check.

**Invariant:** Open community courses: `can_manage_course` is false for teachers—see `permissions.py`.

---

## Runner outputs & artifacts

**Touch together:** `run_code_in_docker` / runner script + `EvaluationResult` path columns + `read_student_safe_submission_artifact_text` + `app/routes/student.py` artifact download route + templates.

**Why:** Paths are relative strings stored in DB; student artifact route whitelists names.

**Failure mode:** File written where DB path does not point; or student download exposes hidden content if sanitization skipped.

---

## LLM groups, quota, admin token policy

**Touch together:** `app/routes/admin.py` + `app/services/llm_groups.py` + `app/services/llm_token_usage.py` + `app/services/llm.py` / `llm_retry.py` (billing checks before calls) + `app/models.py` (`LLMConfig`, `LLMConfigMember`, usage rows).

**Why:** Admin UI changes without service-side enforcement (or vice versa) yield wrong limits or uncaught HTTP errors from provider.

**Failure mode:** UI shows a new group/member but grading still uses old selection or bypasses fallback—check `_resolve_llm_config_for_question` in `submissions.py` and `call_llm_group` in `llm_groups.py`.

**Also verify:** Discussion AI has separate LLM precedence and user-selected `@AI` groups in `app/services/discussion_ai.py`; do not assume grading and discussion AI choose groups identically.

---

## UI wording, enums, status labels

**Touch together:** Templates + `app/i18n.py` + `app/enum_labels.py` + sometimes flash strings inline in routes (`choose_text`, `t(...)`).

**Why:** Enum **values** in DB are English snake_case; user-visible strings are mapped in `enum_labels.py`. Changing a label in only one place confuses bilingual UI.

**Failure mode:** English flash + Chinese page or vice versa.

---

## Database schema

**Touch together:** `app/models.py` + any raw SQL in services + `scripts/init_db.py` / migration habits (if added later).

**Why:** v0 startup creates the current schema from metadata; future deployed upgrades need explicit migration steps.

**Invariant:** Do not add compatibility shims casually to `app/db.py`. Prefer a documented migration script or clearly named idempotent upgrade function.

**Operations:** See `docs/deployment-and-upgrades.md` before schema-changing deployments.

---

## General rule

When a change spans **HTTP + service + template + worker**, run:

`bash scripts/verify.sh`

and manually hit the smallest route that exercises the path if tests do not cover it (see [verification.md](verification.md) for environment-dependent gaps such as Redis, Docker, and live LLM).
