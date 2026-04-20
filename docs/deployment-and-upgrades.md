# Deployment and upgrade notes

This repository is optimized for a small single-machine deployment unless you add stronger infrastructure around it. The notes below summarize the current operational contract in code.

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
- `CODE_QUEUE_NAME`: code-evaluation queue name
- `LLM_QUEUE_PREFIX`: prefix for per-LLM-group queues
- `RUNNER_IMAGE`: default Docker image tag for code evaluation
- `RUNNER_MEMORY_LIMIT`, `RUNNER_CPUS`, `EXECUTION_TIMEOUT_SECONDS`: sandbox limits
- `DOCKER_NETWORK_DISABLED=true` unless a reviewed runner image requires network access
- SMTP settings if email verification or password reset should work

For invitation-code registration, set `REGISTRATION_INVITE_CODE`. Keep it server-side only.

### Configuration reference

| Setting | Purpose | Production guidance |
| --- | --- | --- |
| `SECRET_KEY` | Session signing | Required. Use a long random value and keep it stable. |
| `APP_BASE_URL` | Public origin for email links | Required for email verification and password reset. Use HTTPS. |
| `DATABASE_URL` | SQLAlchemy database URL | Required. SQLite is fine for small single-node deployments. |
| `DATA_DIR` | Uploaded files, outputs, default SQLite | Required. Put it on persistent storage and back it up. |
| `REDIS_URL` | RQ backend | Required for code and LLM evaluation. Web and worker must share it. |
| `CODE_QUEUE_NAME` | Code evaluation queue | Must match web and worker environments. |
| `LLM_QUEUE_PREFIX` | Per-LLM-group queue prefix | Must match web and worker environments. |
| `RUNNER_IMAGE` | Docker image for code questions | Build and deploy this image before accepting code submissions. |
| `DOCKER_NETWORK_DISABLED` | Runner network policy | Keep `true` unless a reviewed runner image and assignment require network. |
| `PDF_REVIEW_MAX_PAGES` | PDF pages sent to multimodal review | Tune for cost and provider limits. |
| SMTP variables | Verification/reset email | Required if email-based registration or password reset should work. |
| `REGISTRATION_INVITE_CODE` | Optional invite registration path | Keep private; do not expose in frontend or public docs. |
| `CSRF_ALLOW_MISSING_ORIGIN_REFERER` | Allow POST without `Origin`/`Referer` | Default unset/false in production. Set to `1` only for API clients or automation that cannot send browser headers. |

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

`worker.py` runs a **manager loop** that spawns and reconciles worker **processes** (each process runs one RQ `Worker` for a single queue name):

- one worker **process** for `CODE_QUEUE_NAME`
- one or more worker **processes** per enabled LLM group, using `LLM_QUEUE_PREFIX-<group_id>`

Each enabled LLM group's `queue_concurrency` controls how many worker processes are created for that group. Inside a group, the worker tries priority #1 first and only uses later LLM members after earlier members fail; the next task starts from #1 again.

If LLM group rows change while the worker is running, the worker manager periodically reconciles the desired queue layout.

## PDF and LLM deployment

PDF submissions are rendered into page images and sent to the file/LLM grading path. Use a model/provider configuration that supports multimodal image inputs for PDF questions.

`PDF_REVIEW_MAX_PAGES` limits how many pages are rendered and sent per submission.

Token usage and daily limits are enforced in `app/services/llm_token_usage.py`. Discussion AI also bills the requesting user.

## Database initialization and migration

Current behavior:

1. `app/main.py` calls `init_database()` at startup.
2. `init_database()` runs `Base.metadata.create_all(bind=engine)`.
3. `init_database()` seeds the open community course.

There is no Alembic migration directory. The current code is a clean v0 baseline and does not preserve removed workflow tables, removed compatibility aliases, or old enum values.

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
- **CSRF:** Unsafe HTTP methods (POST, PUT, PATCH, DELETE) require a matching `Origin` header, or else a `Referer` whose host matches the request (`app/csrf.py`). Same-origin browser forms send at least one of these. If neither header is present, the request is rejected unless `CSRF_ALLOW_MISSING_ORIGIN_REFERER=true`.
- Docker isolation is basic and depends on the configured runner image and Docker flags.
- Uploaded files and artifacts are not automatically cleaned up.

## First-run checklist

1. Create `.env` and set the production values above.
2. Build `RUNNER_IMAGE`.
3. Start Redis.
4. Run `python scripts/init_db.py`.
5. Create a super admin with the deployment bootstrap script or maintenance flow.
6. Start `uvicorn app.main:app` and `python worker.py`.
7. Open `/admin/system` and send an SMTP test if email is enabled.
8. Add and test at least one LLM group if LLM review is enabled.
9. Create a small course, code question, and file/LLM question to verify the full queue path.

## Troubleshooting

| Symptom | Likely cause | What to check |
| --- | --- | --- |
| Students cannot sign in after registration | Email verification is pending | SMTP settings, spam folder, resend verification link |
| Reset or verification link points to the wrong host | `APP_BASE_URL` is wrong | Set it to the public HTTPS origin |
| Code submissions stay queued | Worker is not consuming the same queue | Redis, `worker.py`, `CODE_QUEUE_NAME` |
| Code submissions fail before tests run | Runner cannot start | Docker daemon, `RUNNER_IMAGE`, data-directory permissions |
| LLM submissions stay submitted or fail quickly | No tested callable LLM group | Admin LLM test status, API key, provider URL |
| Users hit quota despite low visible usage | Pre-call budget check blocks large requests | Raise user limit or reduce prompt/page size |
| PDF review misses visual content | Provider is text-only or page cap is low | Multimodal model support, `PDF_REVIEW_MAX_PAGES` |
