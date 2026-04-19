# Architecture: runner and sandbox

## Purpose

Describe how **code questions** execute student programs in an isolated environment and how **artifacts** return to the web app.

**Non-code question types** (PDF/file LLM, short answer LLM) do not use `runner/execute_code.py`; they have separate pipelines in `app/services/submissions.py` + `app/services/llm.py`.

## Components

| Piece | Responsibility |
|-------|------------------|
| `runner/execute_code.py` | Runs inside Docker image: unpack zip, compile/run tests, aggregate stdout/stderr, emit **summary JSON** file. |
| `run_code_in_docker` in `app/services/submissions.py` | Host-side: `docker run` with mounts, invokes runner entrypoint, collects output files into per-submission directory. |
| `process_code_evaluation` | RQ worker job: orchestrates DB status updates, calls `run_code_in_docker`, writes `EvaluationResult` + `Feedback`. |

## Docker & runtime image selection

**Function:** `_resolve_runner_image_tag` (in `submissions.py`) chooses image tag from question → assignment → course default `RuntimeImage`, falling back to `settings.runner_image`.

Admin UI manages `RuntimeImage` records via `app/routes/admin.py`.

## Summary JSON contract

**Source of truth for keys:** `runner/execute_code.py` output + parsing in `process_code_evaluation` / `build_student_result_view`.

Common fields consumed by app code include score aggregates and human-readable `message` (grep `summary_json` in `submissions.py`).

**Do not change field names** in runner JSON without updating all readers and tests (`tests/test_code_runner.py`).

## Artifacts & paths

`EvaluationResult` stores **relative** paths (from data root) for:

- `stdout_path`, `stderr_path`, `log_path` (summary JSON)

Absolute path resolution uses helpers near `get_submission_artifact_path` / `read_submission_artifact_text` in `submissions.py`.

## Student safety: hidden output

`read_student_safe_submission_artifact_text` strips blocks after markers such as `=== Hidden Tests ===` (constants in `submissions.py`).

Changing marker strings requires updating **both** runner emission (if any) and strip logic.

## Legacy notebook execution

`run_job_in_docker` still exists for historical **notebook job** compatibility; it writes a stub failure summary (grep in `submissions.py`). Product redirects old `/jobs` UI via `app/routes/jobs.py`.

**Do not assume** notebook Docker execution equals the modern file/LLM `.ipynb` flow.

The default runner image tag, `notebook-runner-mvp:latest`, is also historical. It is currently the default image for Python, C, and C++ code evaluation.

## Tests

- `tests/test_code_runner.py` — primary integration coverage for runner + DB side effects.

## Common misconceptions

- Thinking **Python-only** — runner supports C/C++ with entrypoints `main.c` / `main.cpp` per `LANGUAGE_ENTRYPOINTS` in `execute_code.py`.
- Editing **`runner/execute_code.py` only** — host-side timeouts, memory, and DB writes live in `submissions.py`.

## Source of truth

1. `runner/execute_code.py` — in-container behavior & summary schema.
2. `app/services/submissions.py` — `run_code_in_docker`, `process_code_evaluation`, artifact IO.
3. `app/models.py` — `CodeQuestionConfig` test JSON, runner limits.

## Coordinated changes

See `docs/change-guide.md` → *Code evaluation & Docker runner* and *Runner outputs & artifacts*.
