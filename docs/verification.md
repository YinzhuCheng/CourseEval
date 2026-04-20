# Verification notes

This page explains **what** the repository’s automated checks cover, **what they do not cover**, and **what to watch** when validating changes—both from a **product / operations** angle and from **architecture / code** angle.

For the exact commands, see `scripts/verify.sh` and [AGENTS.md](../AGENTS.md) § Validation commands.

---

## 1. What runs in CI-style verification (`scripts/verify.sh`)

1. **`python3 -m compileall app runner tests -q`** — Syntax errors and import-time failures in application code, runner, and tests.
2. **`ruff check app runner tests --select F401,F841`** — Unused imports (`F401`) and unused local variables (`F841`) that often indicate dead code or mistakes after refactors.
3. **`python3 -m pytest tests/ -q`** — Full pytest suite (integration-style tests against in-memory SQLite, ASGI clients, etc.).

Install tooling once: `python -m pip install -r requirements-dev.txt`.

---

## 2. Product and deployment scenarios (what automated checks *miss*)

These require **manual or environment-specific** validation:

| Area | Why tests may pass but production fails | What to do |
|------|----------------------------------------|------------|
| **Redis + RQ + worker** | Tests do not start Redis or long-running `worker.py`. | After changes to queues, `enqueue_*`, or `EvaluationTask`, run web + Redis + worker + Docker runner in a staging `.env` and submit a minimal code and LLM question. |
| **Docker / code runner** | `tests/test_code_runner.py` may mock or skip real Docker depending on environment. | Confirm `RUNNER_IMAGE` exists, Docker daemon reachable, and one code submission completes end-to-end. |
| **LLM providers** | Tests often avoid live HTTP to providers. | Use admin LLM connectivity test and a small file/short-answer submission with a real group. |
| **Email (SMTP)** | Registration / reset flows may be exercised with delivery logs or fakes. | Configure SMTP and click through verify / reset links using `APP_BASE_URL` over HTTPS in staging. |
| **CSRF headers** | ASGI tests set `CSRF_ALLOW_MISSING_ORIGIN_REFERER` via `tests/conftest.py`. Production defaults **require** `Origin` or `Referer` on unsafe methods. | Validate forms in a **real browser**; for API clients set `CSRF_ALLOW_MISSING_ORIGIN_REFERER=1` only where intended. |
| **HTTPS / cookies** | Session cookie flags depend on `SESSION_HTTPS_ONLY` and reverse proxy. | Verify login persistence and security headers behind the same TLS termination as production. |
| **Load / SQLite** | Single-node SQLite is assumed for small deployments. | Do not rely on automated tests for concurrent write scaling. |

---

## 3. Architecture and code hotspots (what to re-check when you change X)

Aligns with [change-guide.md](change-guide.md) and [architecture/](architecture/):

- **Submissions and workers** — Any change to `Submission.status`, `EvaluationTask.status`, enqueue, or `process_*` in `app/services/submissions.py` should be paired with a grep of both status enums and, when possible, a test or manual queue run.
- **Scoring and gradebook** — `resolve_submission_score` and `update_final_grade_snapshot` live in `app/services/scoring.py` and `submissions.py`; teacher UI and analytics must stay consistent.
- **Permissions** — Route-level `require_*` / `can_*` are not centralized; grep the same rule for every entrypoint (including POST-only) that performs the action.
- **File serving** — `/data-files/...` in `app/routes/uploads.py` is **whitelist-only**; new upload layouts need an explicit branch and tests if security-relevant.
- **Auth tokens** — Email verification and password reset tokens are stored as **digests** only; do not reintroduce plaintext-token fallbacks without a migration story.

---

## 4. When to extend automated tests

Add or extend tests when you:

- Add a new **question type**, **evaluation task type**, or **queue name**.
- Change **JSON shapes** from the code runner (`runner/execute_code.py`) or LLM grading parsers.
- Introduce a new **permission-sensitive** route or **object-level** access rule.
- Fix a **regression** that slipped through because no test asserted the behavior.

Prefer targeted tests under `tests/` over ad-hoc manual steps when the behavior is deterministic without external services.

---

## 5. Documentation-only changes

If a change touches **only** Markdown or comments, run at least:

```bash
python3 -m compileall app runner -q
```

or document why full `verify.sh` was not run. Prefer full `verify.sh` when documentation changes accompany code.
