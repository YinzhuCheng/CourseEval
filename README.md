# CourseEval / Course Evaluation Platform Skeleton

This repository started as a minimal notebook execution validation system and has been incrementally expanded into a lightweight course-assignment evaluation platform skeleton.

It now supports two layers simultaneously:

1. **Legacy notebook runner flow**  
   `register/login -> upload .ipynb -> enqueue async job -> run notebook inside Docker -> inspect status and results`

2. **Course assignment workflow foundation**  
   `course -> assignment -> question -> per-question submission -> async evaluation -> score/feedback snapshot`

The project still keeps the original notebook runner chain alive so that existing deployments are not broken while the system evolves toward a fuller teaching platform.

## Current scope

Included in the current version:

- User registration
- User login/logout with session cookie
- Legacy notebook upload and execution pages
- Persist notebooks and jobs in SQLite
- Queue jobs in Redis with RQ
- Execute each notebook in an ephemeral Docker container
- Track legacy job states: `queued`, `running`, `success`, `failed`
- View stdout/stderr
- Download executed notebook and HTML export
- Restrict users to only their own legacy jobs and artifacts
- Course model
- Course membership model
- Assignment model
- Question model for:
  - notebook programming questions
  - short-answer questions
- Per-question submission model
- Evaluation task / evaluation result model
- Teacher feedback model
- Final grade snapshot model
- Student pages:
  - my courses
  - assignment detail
  - question detail
  - submission detail
- Teacher pages:
  - course management
  - assignment management
  - question detail
  - submission grading
- Admin pages:
  - user role management
  - runtime image records
  - LLM config records
  - system overview

Out of scope for this MVP:

- Online notebook editing
- Object storage
- PostgreSQL
- Kubernetes
- Multi-machine scheduling
- OAuth / SMS / email verification
- Advanced LLM workflow controls such as retries, moderation review, and cost accounting
- Batch grading workflows
- TA-specific UI refinement
- Rich hidden-test authoring interface

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
- `app/models.py`: legacy runner tables plus expanded course/submission/evaluation tables
- `app/routes/auth.py`: register/login/logout pages and handlers
- `app/routes/jobs.py`: legacy dashboard, upload form, job detail, artifact download
- `app/routes/student.py`: student course / assignment / question / submission pages
- `app/routes/teacher.py`: teacher management and grading pages
- `app/routes/admin.py`: admin records and role pages
- `app/services/jobs.py`: backward-compatible legacy job service wrapper
- `app/services/submissions.py`: new submission/evaluation orchestration
- `app/services/courses.py`: course/assignment/question management helpers
- `app/services/permissions.py`: platform + course role checks
- `worker.py`: RQ worker process
- `runner/execute_notebook.py`: code that runs inside the container to execute/export notebook and write structured summary

## Data model

The repository now contains both legacy and expanded domain tables.

### Legacy runner tables

- `users`
- `notebooks`
- `jobs`
- `job_outputs`

These are kept so existing deployments and existing `/dashboard` + `/jobs/*` flows still work.

### Expanded course evaluation tables

- `courses`
- `course_members`
- `assignments`
- `questions`
- `notebook_question_configs`
- `short_answer_question_configs`
- `runtime_images`
- `llm_configs`
- `submissions`
- `evaluation_tasks`
- `evaluation_results`
- `feedback`
- `final_grade_snapshots`

Important semantics:

- `Submission` is the business submission record
- `EvaluationTask` is the background execution task record
- `EvaluationResult` stores structured automatic evaluation output
- `FinalGradeSnapshot` stores the currently effective grade for a student/question pair
- `failed_system` submissions do **not** count toward limits
- `failed_answer` submissions **do** count toward limits and are considered effective submissions

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
- Notebook timeout: 300 seconds by default
- Docker memory limit: 1G
- Docker CPU limit: 1 core
- Docker network: disabled by default
- Upload size limit: 5 MB
- Intended machine profile: single Ubuntu 22.04 ECS, 2 vCPU / 4 GiB

Note: this version does not implement a strict database-level guard that blocks a user from ever having more than one `running` job if multiple workers are started manually. The intended deployment is a single worker with concurrency 1.

## Environment variables

Copy `.env.example` to `.env` and adjust values if needed.

Important variables:

- `SECRET_KEY`: session signing key, change in production
- `DEFAULT_LOCALE`: default UI language (`en` or `zh`)
- `DATABASE_URL`: SQLite URL
- `REDIS_URL`: Redis connection string
- `RUNNER_IMAGE`: Docker image tag used for notebook execution
- `UPLOAD_MAX_BYTES`: upload size limit
- `EXECUTION_TIMEOUT_SECONDS`: notebook execution timeout

User registration notes:

- all public self-service registrations create student accounts
- the first successful registration bootstraps the initial `super_admin`
- `teacher` and `admin` roles are granted later from inside the system by a `super_admin`
- `scripts/init_super_admin.py` is available as a maintenance tool if you want to seed a dedicated super admin explicitly

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
The first successful registration becomes the bootstrap `super_admin`; all later
registrations are regular student accounts unless a super admin promotes them
inside the platform.

The UI language can be switched from the top-right navigation bar between English and Chinese.

## Legacy notebook runner flow

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

## Course workflow pages

After registration, the system bootstraps a **Demo Course** for the new user to make the expanded flow visible immediately.

### Student pages

- `/student/courses`
- `/student/assignments/{id}`
- `/student/questions/{id}`
- `/student/submissions/{id}`

Student course page now also supports:

- joining a course by join code
- viewing assignment schedules in the configured UI timezone
- notebook submission auto-queueing for automatic evaluation

### Teacher pages

- `/teacher/courses`
- `/teacher/courses/{id}`
- `/teacher/assignments/{id}`
- `/teacher/questions/{id}`
- `/teacher/submissions/{id}`

Teacher workflow currently supports:

- creating courses
- sharing a generated course join code with students
- creating assignments
- creating notebook / short-answer questions
- configuring visible tests / hidden tests source text
- configuring scoring rule and basic submission limits
- reviewing submissions and overriding scores/comments

### Admin pages

- `/admin/users`
- `/admin/runtime-images`
- `/admin/llm-configs`
- `/admin/system`

The first registered user becomes platform admin automatically.

## Expanded submission flow

For notebook questions:

1. Student opens a question
2. Student uploads `.ipynb`
3. System creates `Submission`
4. System creates `EvaluationTask`
5. Worker automatically runs isolated Docker evaluation
6. System writes `EvaluationResult`
7. If enabled, worker also enqueues notebook LLM feedback generation
8. System updates `FinalGradeSnapshot`

For short-answer questions:

1. Student submits text
2. System creates `Submission`
3. If enabled, worker runs an LLM suggestion task
4. Teacher reviews and can override the suggested score/comment
5. Teacher feedback updates `FinalGradeSnapshot`

## Evaluation result semantics

Submission statuses:

- `submitted`
- `queued`
- `running`
- `completed`
- `failed_system`
- `failed_answer`

Evaluation task statuses:

- `queued`
- `running`
- `succeeded`
- `failed`

Score authority:

1. Teacher score
2. LLM suggestion / automatic score
3. No score

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

Submission evaluation artifacts are stored under:

- `data/outputs/submissions/submission-<submission_id>/`

Each submission output directory stores:

- `executed.ipynb`
- `executed.html`
- `stdout.txt`
- `stderr.txt`
- `summary.json`

## Timezone and language behavior

- UI language can be switched globally between English and Chinese from the navigation bar
- default language comes from `DEFAULT_LOCALE`
- template-rendered timestamps are formatted in Asia/Shanghai (Beijing time)

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

The expanded submission flow still uses the same Docker-isolated execution principle; only the surrounding business objects changed from legacy `Job` to `Submission + EvaluationTask + EvaluationResult`.

## Web pages

Implemented pages include:

- `/register`
- `/login`
- `/dashboard`
- `/jobs/new`
- `/jobs/{id}`
- `/student/courses`
- `/student/assignments/{id}`
- `/student/questions/{id}`
- `/student/submissions/{id}`
- `/teacher/courses`
- `/teacher/courses/{id}`
- `/teacher/assignments/{id}`
- `/teacher/questions/{id}`
- `/teacher/submissions/{id}`
- `/admin/users`
- `/admin/runtime-images`
- `/admin/llm-configs`
- `/admin/system`

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
- LLM connectivity test is currently a structural smoke-test, not a live provider call
- Runtime image records exist, but per-course runtime enforcement is still basic
- No per-user storage quota
- No background cleanup for old artifacts
- No advanced sandbox hardening beyond Docker flags
- Intended for a single worker process; not tuned for parallel execution
- HTML output is served as generated and not sanitized beyond access control
- Teacher and student can currently overlap through course membership simplifications
- Queue backend is still RQ; migration to Celery is a future evolution step

## Future expansion ideas

Without overhauling the MVP, the current structure can later support:

- PostgreSQL migration
- richer job logs
- job retry controls
- admin inspection page
- object storage
- prebuilt runner image pipelines
- stricter per-user concurrency enforcement

