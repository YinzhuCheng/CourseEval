# Deployment and upgrade notes

This repository is optimized for a small single-machine deployment unless you add stronger infrastructure around it. The notes below summarize the current operational contract in code.

### Python dependency layers

Install targets are split on purpose:

| Layer | File | Role |
|-------|------|------|
| **1 — Application runtime** | `requirements.txt` | CourseEval web + `worker.py` on the host. |
| **2 — Dev / CI / tests** | `requirements-dev.txt` | Layer 1 plus `pytest`. Use on build agents before `bash scripts/verify.sh`. |
| **3 — Student code sandbox** | `runner/requirements.txt` + `runner/Dockerfile` | Packages inside the **code runner** Docker image only. |

`bash scripts/verify.sh` checks **application** code and tests (Layers 1–2). It is **not** a substitute for rebuilding and smoke-testing the runner image when `runner/` or Layer 3 packages change.

---

## Runtime services

Run these services against the same `.env` values:

- FastAPI web app: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Redis: used by RQ queues
- Worker manager: `python worker.py`
- Docker daemon: required for code questions

The web app initializes data directories and schema on startup through `app/main.py`.

## Required persistent state

Persist and back up:

- SQLite database, by default under `DATA_DIR/app.db` unless `DATABASE_URL` points elsewhere
- uploaded files under `DATA_DIR/uploads`
- evaluation artifacts under `DATA_DIR/outputs`
- any custom runner Docker images referenced by `RuntimeImage` records

Keep `SECRET_KEY` stable across restarts. Rotating it invalidates existing sessions.

## Environment checklist

Minimum production-like values:

- `SECRET_KEY`: long random string, not the default
- `APP_BASE_URL`: public HTTPS origin used in verification/reset email links
- `DATABASE_URL`: database location
- `DATA_DIR`: persistent directory
- `REDIS_URL`: Redis reachable from web and worker processes
- `PYTHON_QUEUE_NAME`: code-evaluation queue name
- `LLM_QUEUE_PREFIX`: prefix for per-LLM-config queues
- `RUNNER_IMAGE`: default Docker image tag for code evaluation
- `RUNNER_MEMORY_LIMIT`, `RUNNER_CPUS`, `EXECUTION_TIMEOUT_SECONDS`: sandbox limits
- `DOCKER_NETWORK_DISABLED=true` unless a reviewed runner image requires network access
- SMTP settings if email verification or password reset should work

For invitation-code registration, set `REGISTRATION_INVITE_CODE`. Keep it server-side only.

## Runner image

Build the default runner image before accepting code submissions:

```bash
docker build -t courseeval-runner:latest runner
```

This image is used for Python, C, and C++ code evaluation.

When changing the default Python package set, update both:

- `runner/requirements.txt`
- `app/runtime_support.py`

Then rebuild and redeploy the runner image.

## Worker queues

`worker.py` starts:

- one worker for `PYTHON_QUEUE_NAME`
- one or more workers per enabled `LLMConfig`, using `LLM_QUEUE_PREFIX-<config_id>`

Each enabled LLM config's `queue_concurrency` controls how many worker processes are created for that config.

If LLM config rows change while the worker is running, the worker manager periodically reconciles the desired queue layout.

## PDF and LLM deployment

PDF submissions are rendered into page images and sent to the file/LLM grading path. Use a model/provider configuration that supports multimodal image inputs for PDF questions.

`PDF_REVIEW_MAX_PAGES` limits how many pages are rendered and sent per submission.

Token usage and daily limits are enforced in `app/services/llm_token_usage.py`. Discussion AI also bills the requesting user.

## Database initialization and migration

Current behavior:

1. `app/main.py` calls `init_database()` at startup.
2. `init_database()` runs `Base.metadata.create_all(bind=engine)`.
3. `init_database()` seeds the open community course.

There is no Alembic migration directory. The current code is a clean v0 baseline and does not preserve retired notebook/job tables or old question enum values.

Before production upgrades:

1. Stop web and worker processes, or otherwise prevent writes.
2. Back up the database and `DATA_DIR`.
3. Run the new code against a copy of the database.
4. Verify `python scripts/init_db.py` completes successfully on the copy.
5. Run `bash scripts/verify.sh` in an environment with `requirements-dev.txt` installed.
6. Start web and worker processes with the same `.env`.
7. Submit a small code question and, if enabled, a small LLM/file question to verify queues and artifacts.

For future schema changes with real deployments, choose one of these patterns before shipping:

- Add a focused migration script under `scripts/` and document the exact command here.
- Add a clearly named, idempotent upgrade function called by `init_database()` only when it is safe on existing data.
- Adopt a formal migration tool such as Alembic and document the revision command.

Avoid assuming `create_all` modifies existing columns or constraints; it does not.

## Rollback notes

Rollback is safest from a database and `DATA_DIR` backup taken before startup with the new code.

Code rollback without data rollback can leave old code reading newer values after schema or enum changes. Prefer restoring the database and `DATA_DIR` backup taken before startup with the new code.

## Security and deployment caveats

- Put HTTPS in front of the app.
- The current session middleware sets `https_only=False`; if deployed behind HTTPS, review cookie settings before hardening.
- SQLite is intended for small single-node use.
- There is no CSRF protection layer yet.
- Docker isolation is basic and depends on the configured runner image and Docker flags.
- Uploaded files and artifacts are not automatically cleaned up.
