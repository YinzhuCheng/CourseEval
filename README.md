# Notebook Runner MVP

Notebook Runner MVP is a minimal notebook execution validation system built for a single Ubuntu 22.04 machine. It focuses on one critical flow only:

`register/login -> upload .ipynb -> enqueue async job -> run notebook inside Docker -> inspect status and results`

This project is intentionally small and practical. It is not an online notebook editor, not a teaching platform, and not a multi-node scheduler.

## MVP scope

Included in this version:

- User registration
- User login/logout with session cookie
- Upload `.ipynb` files
- Persist notebooks and jobs in SQLite
- Queue jobs in Redis with RQ
- Execute each notebook in an ephemeral Docker container
- Track job states: `queued`, `running`, `success`, `failed`
- View stdout/stderr
- Download executed notebook and HTML export
- Restrict users to only their own jobs and artifacts

Out of scope for this MVP:

- Online notebook editing
- Teacher/student roles
- Assignments, grading, courses
- Object storage
- PostgreSQL
- Kubernetes
- Multi-machine scheduling
- OAuth / SMS / email verification

## Tech stack

- FastAPI
- Jinja2 server-rendered pages
- SQLAlchemy ORM
- SQLite
- Redis
- RQ
- Docker
- nbclient / nbconvert / jupyter kernel inside runner image
- Bootstrap 5

## Project structure

```text
.
├── app/
│   ├── auth.py
│   ├── config.py
│   ├── constants.py
│   ├── db.py
│   ├── env.py
│   ├── main.py
│   ├── models.py
│   ├── routes/
│   │   ├── auth.py
│   │   └── jobs.py
│   ├── services/
│   │   └── jobs.py
│   ├── static/
│   │   └── style.css
│   ├── templates/
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── job_detail.html
│   │   ├── login.html
│   │   ├── new_job.html
│   │   └── register.html
│   └── __init__.py
├── data/
│   ├── outputs/
│   └── uploads/
├── runner/
│   ├── Dockerfile
│   └── execute_notebook.py
├── samples/
│   └── minimal_demo.ipynb
├── scripts/
│   └── init_db.py
├── worker.py
├── requirements.txt
├── .env.example
└── README.md
```

## Key modules

- `app/main.py`: FastAPI app entrypoint, middleware, startup initialization
- `app/models.py`: `users`, `notebooks`, `jobs`, `job_outputs` schema definitions
- `app/routes/auth.py`: register/login/logout pages and handlers
- `app/routes/jobs.py`: dashboard, upload form, job detail, artifact download
- `app/services/jobs.py`: upload persistence, RQ queueing, Docker runner orchestration, artifact handling
- `worker.py`: RQ worker process
- `runner/execute_notebook.py`: code that runs inside the container to execute/export notebook

## Data model

The application uses four core tables:

### `users`

- `id`
- `username` (unique)
- `email` (unique)
- `password_hash`
- `created_at`

### `notebooks`

- `id`
- `user_id`
- `original_filename`
- `stored_path`
- `uploaded_at`

### `jobs`

- `id`
- `user_id`
- `notebook_id`
- `status`
- `created_at`
- `started_at`
- `finished_at`
- `exit_code`
- `error_message`

### `job_outputs`

- `id`
- `job_id`
- `executed_notebook_path`
- `html_path`
- `stdout_path`
- `stderr_path`

Indexes and foreign keys are included for the main lookup paths.

## Runtime constraints

Default runtime settings target a small single machine:

- Worker concurrency: 1 process
- Notebook timeout: 300 seconds
- Docker memory limit: 1G
- Docker CPU limit: 1 core
- Docker network: disabled by default
- Upload size limit: 5 MB

Note: this version does not implement a strict database-level guard that blocks a user from ever having more than one `running` job if multiple workers are started manually. The intended deployment is a single worker with concurrency 1.

## Environment variables

Copy `.env.example` to `.env` and adjust values if needed.

Important variables:

- `SECRET_KEY`: session signing key, change in production
- `DATABASE_URL`: SQLite URL
- `REDIS_URL`: Redis connection string
- `RUNNER_IMAGE`: Docker image tag used for notebook execution
- `UPLOAD_MAX_BYTES`: upload size limit
- `EXECUTION_TIMEOUT_SECONDS`: notebook execution timeout

## Local startup

### 1. Create Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Prepare environment file

```bash
cp .env.example .env
```

Edit `SECRET_KEY` before real deployment.

### 3. Initialize database

```bash
python scripts/init_db.py
```

This creates `data/app.db` plus upload/output directories.

### 4. Start Redis

If Redis is already installed on Ubuntu:

```bash
redis-server --save "" --appendonly no
```

Or with Docker:

```bash
docker run --rm -p 6379:6379 redis:7-alpine
```

### 5. Build the runner image

```bash
docker build -t notebook-runner-mvp:latest runner
```

This image contains the minimal notebook execution stack used by each job container.

### 6. Start the web service

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 7. Start the worker

Open another shell:

```bash
source .venv/bin/activate
python worker.py
```

## Startup order

Recommended startup order:

1. Redis
2. Build runner image
3. Initialize database
4. Web service
5. Worker

## Creating the first user

Open the site in a browser:

- `http://127.0.0.1:8000/register`

Then fill:

- username
- email
- password
- confirm password

Registration logs you in immediately.

## Uploading a notebook

After login:

1. Open `/jobs/new`
2. Upload an `.ipynb` file
3. Submit
4. The app creates:
   - a notebook record
   - a queued job record
   - output metadata
   - an RQ job in Redis

The dashboard and detail page auto-refresh every 5 seconds while the job is active.

## Minimal validation flow

Use the included sample notebook:

- `samples/minimal_demo.ipynb`

Validation flow:

1. Register a user
2. Log in
3. Upload `samples/minimal_demo.ipynb`
4. Open the job detail page
5. Wait for status to move:
   - `queued`
   - `running`
   - `success`
6. Verify:
   - stdout is visible
   - stderr is visible or empty
   - executed notebook can be downloaded
   - HTML result opens in a new tab or can be downloaded

## Where files are stored

- Original uploads: `data/uploads/user-<user_id>/`
- Job outputs: `data/outputs/job-<job_id>/`

Each job output directory stores:

- `executed.ipynb`
- `executed.html`
- `stdout.txt`
- `stderr.txt`

## Redis usage

This project uses Redis only as the RQ backend. No extra result store or cache layer is required.

Queue name default:

- `notebook-jobs`

## Docker runner design

Each notebook job is executed with a one-off `docker run` call from the worker:

- `--rm`
- `--memory 1g`
- `--cpus 1`
- `--pids-limit 256`
- `--network none` by default
- mount only:
  - the input notebook (read-only)
  - the output directory

The host Python environment never executes user notebook code directly.

## Web pages

Implemented pages:

- `/register`
- `/login`
- `/dashboard`
- `/jobs/new`
- `/jobs/{id}`

## Error handling covered

The MVP includes readable handling for:

- unauthenticated access
- invalid login
- duplicate username/email
- non-`.ipynb` upload
- oversized upload
- queue submission failure
- Docker missing/unavailable
- notebook timeout
- notebook execution failure
- HTML export failure
- missing result artifact
- access to another user’s job/artifacts
- stale `running` jobs after worker restart

## Common issues

### 1. Worker never picks up jobs

Check:

- Redis is running
- `REDIS_URL` is correct
- `python worker.py` is running

### 2. Job fails immediately with Docker-related error

Check:

- Docker is installed
- current user can run `docker`
- runner image exists:

```bash
docker images | rg notebook-runner-mvp
```

### 3. HTML or executed notebook missing

Inspect:

- job detail stderr
- worker logs
- `data/outputs/job-<id>/`

### 4. Session login does not persist

Check:

- `SECRET_KEY` is set
- browser accepts cookies

## Single-machine ECS deployment notes

For a 2C4G Ubuntu 22.04 ECS instance, recommended deployment shape:

- 1 FastAPI web process
- 1 RQ worker process
- 1 local Redis instance
- Docker installed on the host
- SQLite stored on local disk

Suggested production additions:

- run web and worker under `systemd`
- place Nginx in front as reverse proxy
- terminate TLS at Nginx
- restrict inbound security group rules
- move `data/` onto persistent disk
- rotate logs with `journald` or logrotate

Example reverse proxy direction:

- Nginx -> `127.0.0.1:8000`

## Known limitations

- SQLite is suitable only for small single-node usage
- No CSRF protection layer yet
- No admin page
- No notebook image allowlist/version management
- No per-user storage quota
- No background cleanup for old artifacts
- No advanced sandbox hardening beyond Docker flags
- Intended for a single worker process; not tuned for parallel execution
- HTML output is served as generated and not sanitized beyond access control

## Future expansion ideas

Without overhauling the MVP, the current structure can later support:

- PostgreSQL migration
- richer job logs
- job retry controls
- admin inspection page
- object storage
- prebuilt runner image pipelines
- stricter per-user concurrency enforcement

