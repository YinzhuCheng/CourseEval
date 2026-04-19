# Architecture: submission lifecycle

## Purpose

Track how student work moves from **HTTP upload** to **persisted `Submission`** to **background evaluation** and **visible results**.

## Key entities (ORM)

| Model | Role |
|-------|------|
| `Submission` | One student attempt for a question; holds status, file paths, text answer, flags (`counts_toward_limit`, `is_effective_submission`, …). |
| `EvaluationTask` | Unit of async work (code run, LLM eval, …); carries `task_type`, `status`, `backend_job_id`. |
| `EvaluationResult` | Immutable-ish outcome row (scores, paths to artifacts, `summary_json`). |
| `Feedback` | Comment + optional score suggestion; `source` = auto / LLM / teacher. |

Source: `app/models.py`.

## Entrypoints (HTTP)

| Area | Module |
|------|--------|
| Student uploads / text submit | `app/routes/student.py` — grep `submit-` path segments. |

Routes should delegate quickly to `app/services/submissions.py`.

## Core orchestration (service)

**File:** `app/services/submissions.py`

Representative functions (grep for full list):

- `create_code_submission`, `create_file_submission`, `create_short_answer_submission`, `create_notebook_submission`, …
- `enqueue_submission_evaluation`, `enqueue_file_llm_evaluation`, `enqueue_short_answer_llm`, …
- `process_code_evaluation`, `process_submission_evaluation` (legacy notebook docker path), `process_file_llm_evaluation`, …

## Queue / worker

| Component | Role |
|-----------|------|
| `enqueue_*` | Pushes RQ jobs, sets `EvaluationTask.backend_job_id`, updates submission to `queued` where applicable. |
| `worker.py` | Spawns worker processes for Python queue + per-`LLMConfig` LLM queues (`llm_queue_name_for_config`). |

**Invariant:** `enqueue_submission_evaluation` chooses `process_code_evaluation` vs `process_submission_evaluation` based on `EvaluationTaskType` (grep `if task.task_type`).

## Status transitions (high level)

Exact rules live in `submissions.py`; do not duplicate here as prose without re-reading code.

**Submission (`SubmissionStatus`):**

- Typical code path: `submitted` → `queued` → `running` → `completed` | `failed_system` | `failed_answer`.
- `failed_system` vs `failed_answer` affects whether attempt counts toward limit—see `process_code_evaluation` exception handling and Docker-missing branch.

**Evaluation task (`EvaluationTaskStatus`):**

- `queued` → `running` → `succeeded` | `failed` (worker updates).

## Artifacts

After code evaluation, `EvaluationResult` stores **relative** paths (under configured data root) for stdout, stderr, summary JSON. Student-facing routes read via `read_student_safe_submission_artifact_text` to strip hidden blocks.

## Templates

| Page | Template |
|------|----------|
| Student submission detail | `app/templates/student_submission_detail.html` |
| Teacher submission review | `app/templates/teacher_submission_detail.html` |

## Tests

- `tests/test_code_runner.py` — code path integration.
- File / LLM: `tests/test_file_llm_image_inputs.py` and related.

## Common misconceptions

- **`QuestionType.NOTEBOOK` vs file-upload `.ipynb`:** product may still have “notebook” typed questions for LLM workflows while **Docker notebook execution** is legacy/stubbed—verify `create_*` and `process_*` for the question type you touch.
- **Route success ≠ evaluation done:** creation endpoints often return redirect while worker runs asynchronously.

## Source of truth

- Primary: `app/services/submissions.py`
- Schema: `app/models.py`
- HTTP: `app/routes/student.py`

## Coordinated changes

See `docs/change-guide.md` sections: *Submission creation*, *Queue task state*, *Runner outputs*.
