# CourseEval

CourseEval is a lightweight course-assignment evaluation platform for teaching teams.

The current product direction is:

- **Code evaluation** for executable Python, C, and C++ programming questions
- **File / LLM-reviewed evaluation** for `.pdf`, `.txt`, `.tex`, `.md`, and `.ipynb` submissions
- **Teacher-confirmed grading** for workflows where automatic suggestions should not directly become final grades

The v0 baseline keeps only three active question types: code, short answer, and file / LLM-reviewed upload.

## Repository map for coding agents

This repository includes **layered documentation for AI coding agents** (and for humans who want the same navigation hints). Those docs exist because agent workflows benefit from **explicit subsystem boundaries, invariants, and “edit these files together” guidance**—things that are easy to miss when scanning filenames or a single long README.

- **README** stays the human-friendly entrypoint for what the product does.
- **[AGENTS.md](AGENTS.md)** is the top-level map for agents: stack, subsystems, workflows, traps, validation commands.
- **[docs/file-map.md](docs/file-map.md)** groups important files by responsibility (where to look first).
- **[docs/change-guide.md](docs/change-guide.md)** explains common change couplings and failure modes.
- **[docs/known-issues.md](docs/known-issues.md)** records confirmed technical debt, historical compatibility traps, and review notes.
- **[docs/deployment-and-upgrades.md](docs/deployment-and-upgrades.md)** summarizes deployment, persistence, queue, and migration/upgrade expectations.
- **[docs/architecture/](docs/architecture/)** holds deeper, retrieval-friendly notes on submission flow, scoring, permissions, and the code runner.

Please read **AGENTS.md** and the relevant `docs/` pages **before making non-trivial code changes**, especially when touching submissions, evaluation, scoring, permissions, or the Docker runner.

## What the system supports

### Student workflows

- Register and sign in
- Join a course with a join code
- View courses, assignments, questions, and submission history
- Submit:
  - `.py`, `.c`, `.cpp`, `.cc`, `.cxx`, or `.zip` files for code questions
  - `.pdf`, `.txt`, `.tex`, `.md`, or `.ipynb` files for file / LLM-reviewed questions
- View evaluation progress, feedback, and downloadable artifacts
- Read built-in code runtime help inside the product UI

### Teacher workflows

- Create and manage courses
- Share course join codes with students
- Create assignments
- Create question types for:
  - Python, C, and C++ code evaluation
  - file upload / LLM-reviewed evaluation with configurable `.pdf`, `.txt`, `.tex`, `.md`, and `.ipynb` extensions
- Configure code test cases, allowed language sets, reference solutions, scoring rules, and submission limits
- Review submissions and confirm final grades

### Admin workflows

- Manage user roles
- Manage runtime image records
- Manage LLM configuration records
- Review system overview data

## Supported code runtime

The default code evaluation runtime currently provides:

- **Python** `3.12`
- **C** `C11` through `gcc`
- **C++** `C++17` through `g++`

### Preinstalled Python packages

| Package | Version |
| --- | --- |
| `numpy` | `2.4.4` |
| `pandas` | `3.0.2` |
| `matplotlib` | `3.10.8` |
| `scipy` | `1.17.1` |
| `scikit-learn` | `1.8.0` |

Notes:

- Python standard library imports are supported
- C submissions may use the C11 standard library
- C++ submissions may use the C++17 standard library
- Third-party C/C++ libraries, Makefile, CMake, and custom build commands are not supported by the default runner
- The default runtime image does **not** preinstall deep-learning frameworks such as `torch`, `tensorflow`, `jax`, `paddle`, `mxnet`, or `transformers`
- Students can also view the same information inside the product at `/student/help/python-runtime`
- The UI package support matrix is defined in `app/runtime_support.py`
- The runner Docker image installs Python packages from `runner/requirements.txt`; update both files and rebuild the runner image when expanding the preinstalled Python package set

## Submission model

### 1. Code questions

Use this when students should submit executable Python, C, or C++ code and receive test-based automatic evaluation.

Typical flow:

1. Teacher creates a **code** question
2. Teacher chooses the allowed language set: Python, C, C++, or any combination
3. Teacher configures visible and hidden test cases
4. Teacher may provide reference solutions for Python, C, and C++
5. Student chooses one allowed language and uploads a source file or zip archive
6. The worker runs the code in an isolated Docker container
7. The system stores structured evaluation results and feedback
8. The final grade snapshot is updated

Submission rules:

- Single-file Python submissions use `.py`
- Single-file C submissions use `.c`
- Single-file C++ submissions use `.cpp`, `.cc`, or `.cxx`
- Multi-file submissions use `.zip`
- Zip submissions must contain `main.py`, `main.c`, or `main.cpp` as the entry file
- Programs read from standard input and write answers to standard output

### 2. File / LLM-reviewed questions

Use this when students should submit a report, analysis, explanation, or notebook-style artifact.

Supported submission file types:

- `.pdf`
- `.txt`
- `.tex`
- `.md`
- `.ipynb`

For `.ipynb`:

- `.ipynb` is supported through the **file / LLM-reviewed** route
- Teachers can require notebooks to already contain executed outputs before upload

Typical flow:

1. Teacher creates a **file upload / LLM-reviewed** question and chooses allowed file extensions
2. Teacher provides rubric text and reference answer guidance
3. Student uploads a supported file
4. The system extracts readable content
5. The worker produces an LLM review suggestion
6. Teacher confirms or adjusts the final result
7. The final grade snapshot is updated

## Architecture overview

- **FastAPI** application
- **Jinja2** server-rendered UI
- **SQLAlchemy** ORM
- **SQLite** as the default database
- **Redis + RQ** for background tasks
- **Docker** for isolated execution of code questions
- **Bootstrap 5** for the UI

## Important modules

- `app/main.py`: FastAPI entrypoint
- `app/routes/auth.py`: registration, login, locale switching
- `app/routes/student.py`: student pages and submission entrypoints
- `app/routes/teacher.py`: teacher course, assignment, question, and grading pages
- `app/routes/admin.py`: admin pages and runtime / LLM configuration pages
- `app/services/submissions.py`: submission orchestration and background evaluation logic
- `app/services/scoring.py`: effective score resolution and teacher-confirmation gating
- `app/services/storage_paths.py`: shared data-directory path safety helpers
- `app/services/courses.py`: course, assignment, and question helpers
- `app/runtime_support.py`: canonical language and package support matrix used by the help UI
- `runner/requirements.txt`: Python packages installed into the default runner image
- `runner/execute_code.py`: helper script used inside the code runner container
- `runner/Dockerfile`: default runtime image definition
- `worker.py`: background worker process

## Project structure

```text
.
├── app/
│   ├── constants.py
│   ├── db.py
│   ├── main.py
│   ├── models.py
│   ├── runtime_support.py
│   ├── routes/
│   │   ├── admin.py
│   │   ├── auth.py
│   │   ├── course_content.py
│   │   ├── student.py
│   │   ├── teacher.py
│   │   └── uploads.py
│   ├── services/
│   │   ├── courses.py
│   │   ├── llm.py
│   │   ├── permissions.py
│   │   ├── scoring.py
│   │   ├── storage_paths.py
│   │   └── submissions.py
│   ├── static/
│   │   └── style.css
│   └── templates/
├── data/
├── docs/
├── runner/
│   ├── Dockerfile
│   ├── execute_code.py
│   └── requirements.txt
├── scripts/
├── tests/
├── AGENTS.md
├── worker.py
├── requirements.txt
└── README.md
```

## Local development

### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install development dependencies

```bash
python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes the app dependencies plus the test runner used by the shared verification script.

### 3. Prepare environment file

```bash
cp .env.example .env
```

Update `SECRET_KEY` before real deployment.

### 4. Initialize the database

```bash
python scripts/init_db.py
```

### 5. Run local verification

```bash
bash scripts/verify.sh
```

This is the standard verification entrypoint for local development, Codex, and other agents. It runs Python compilation checks and the pytest suite.

### 6. Start Redis

If Redis is already installed:

```bash
redis-server --save "" --appendonly no
```

Or with Docker:

```bash
docker run --rm -p 6379:6379 redis:7-alpine
```

### 7. Build the default runner image

```bash
docker build -t courseeval-runner:latest runner
```

This image contains the default Docker-isolated code runtime used for Python, C, and C++ evaluation. To add Python packages to the default runtime, update both `app/runtime_support.py` and `runner/requirements.txt`, then rebuild this image.

### 8. Start the web service

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 9. Start the worker

In another shell:

```bash
python worker.py
```

## Runtime and operational defaults

- Worker concurrency: 1 process
- Python execution timeout: 300 seconds by default
- Docker memory limit: 1 GB
- Docker CPU limit: 1 core
- Docker network: disabled by default
- Upload size limit: 5 MB
- Intended deployment profile: small single-machine Ubuntu setup

## Registration deployment notes

Public registration now supports two paths:

1. **Email verification registration**
   - The user submits the registration form with an email address
   - The system creates a pending account with `email_verified = false`
   - The system sends a verification email containing `/verify-email?token=...`
   - The user clicks the email link
   - The account becomes active and can sign in
2. **Invitation-code registration**
   - The user selects invitation-code registration
   - The server validates the configured invitation code
   - The account becomes active immediately
   - If the user leaves the email blank, the system generates an internal placeholder email and the user can still sign in with the username

### Required environment variables

Set these values in `.env` for production:

```bash
APP_BASE_URL=https://your-domain.example.com
REGISTRATION_INVITE_CODE=your-server-side-invite-code
INTERNAL_EMAIL_DOMAIN=invite.local
EMAIL_VERIFICATION_EXPIRE_HOURS=24

SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-smtp-user
SMTP_PASSWORD=your-smtp-password
SMTP_FROM_ADDRESS=no-reply@your-domain.example.com
SMTP_FROM_NAME=CourseEval
SMTP_STARTTLS=true
SMTP_USE_SSL=false
```

### Deployment checklist

- `APP_BASE_URL` must be the externally accessible HTTPS origin used by end users, otherwise verification links may point to an internal address
- Configure a working SMTP account if you want email-verification registration to work in production
- Set `REGISTRATION_INVITE_CODE` if you want to allow the faster invitation-code registration path
- Keep the invitation code only on the server side; do not expose it in client-side assets or public docs
- `INTERNAL_EMAIL_DOMAIN` should use a reserved internal-only domain because it is used for auto-generated placeholder emails when invite registrations skip the email field
- Use HTTPS in front of the FastAPI app so email verification links and session cookies travel securely
- Make sure the email sender domain and mailbox are allowed by your mail provider
- If you run multiple app instances, they must share the same database so verification tokens stay valid across nodes
- Keep `SECRET_KEY` stable across restarts so session handling remains consistent

### Behavior when SMTP is not configured

If SMTP is missing or delivery fails, email-based registrations stay pending until email sending works and the verification email is resent. Invitation-code registrations are not affected as long as `REGISTRATION_INVITE_CODE` is configured.

### Email operations

- Password reset is available from the login page for verified, active accounts
- Verification and password reset tokens are stored as server-keyed hashes in the database
- Verification resend and password reset requests have a short cooldown to reduce email abuse
- Admins can send a test message from **Admin → System overview → Email tools**
- Email delivery attempts are recorded in `email_delivery_logs` for audit and troubleshooting

## Queue and LLM deployment notes

The platform now separates evaluation traffic into:

- a dedicated code evaluation queue
- per-LLM-config queues for LLM review tasks

Relevant environment variables:

```bash
PYTHON_QUEUE_NAME=python-evaluations
LLM_QUEUE_PREFIX=llm-evaluations
PDF_REVIEW_MAX_PAGES=8
```

### Worker behavior

- Docker code grading runs on its own queue
- Each enabled LLM config has its own queue
- `queue_concurrency` is configured per LLM config record in the admin UI
- `worker.py` now works as a lightweight worker manager and starts:
  - 1 Python worker process
  - N LLM worker processes per enabled config, where N is that config's concurrency

### Global default LLM behavior

- The latest **platform** LLM config that has passed connectivity testing becomes the default for all courses still following the global platform default
- Teachers can override a specific course to use a chosen enabled LLM config from the course detail page

### PDF review behavior

- Uploaded PDFs are rendered page-by-page into images for grading
- Those page images are sent to the configured multimodal LLM
- Reference-answer PDFs may still be text-extracted for prompt context
- `PDF_REVIEW_MAX_PAGES` limits how many pages are rendered per submission
- Use a multimodal-capable model for PDF review; text-only models may fail for these tasks

## Deployment and upgrades

For a fuller operations checklist, see **[docs/deployment-and-upgrades.md](docs/deployment-and-upgrades.md)**.

High-level deployment requirements:

- Persist `DATA_DIR` and the database; default SQLite lives under `DATA_DIR/app.db`
- Run web, Redis, worker, Docker, and the runner image together
- Set `SECRET_KEY`, `APP_BASE_URL`, `DATABASE_URL`, `REDIS_URL`, `PYTHON_QUEUE_NAME`, `LLM_QUEUE_PREFIX`, and runner limits explicitly for production
- Configure SMTP if email verification or password reset should work
- Back up the database and `DATA_DIR` before upgrades

Migration / upgrade summary:

- Startup runs `init_database()`, which calls `metadata.create_all()` and seeds the open community course
- There is no Alembic migration tree in this repository
- `create_all()` will not rewrite existing columns or constraints; schema changes need an explicit migration script, an idempotent upgrade function, or a documented Alembic adoption
- Run `bash scripts/verify.sh` in an environment with `requirements-dev.txt` installed
- Test upgrades against a copy of production data before deploying

## Data model notes

Main teaching-domain tables include:

- `courses`
- `course_members`
- `assignments`
- `questions`
- `code_question_configs`
- `file_question_configs`
- `short_answer_question_configs`
- `runtime_images`
- `llm_configs`
- `submissions`
- `evaluation_tasks`
- `evaluation_results`
- `feedback`
- `final_grade_snapshots`

## Artifacts

Submission evaluation artifacts are stored under:

- `data/outputs/submissions/submission-<submission_id>/`

Depending on question type, artifacts may include:

- `stdout.txt`
- `stderr.txt`
- `summary.json`
- `executed.ipynb`
- `executed.html`

## Language behavior

- The UI supports both **English** and **Chinese**
- Users can switch language from the top navigation bar
- Default locale comes from `DEFAULT_LOCALE`

## Common issues

### Worker does not process tasks

Check:

- Redis is running
- `REDIS_URL` is correct
- `python worker.py` is running

### Code evaluation fails immediately with Docker-related errors

Check:

- Docker is installed
- The current user can run `docker`
- The runner image exists:

```bash
docker images | rg courseeval-runner
```

### Session login does not persist

Check:

- `SECRET_KEY` is set
- Browser cookies are enabled

## Current limitations

- SQLite is suitable only for small single-node usage
- No CSRF protection layer yet
- LLM connectivity testing is still a structural smoke test, not a live provider guarantee
- Runtime image records exist, but per-course runtime enforcement is still basic
- No per-user storage quota
- No automatic cleanup for old artifacts
- No advanced sandbox hardening beyond Docker flags
- Intended for a single worker process and small-scale deployments

## Upgrade and migration policy

CourseEval is currently treated as a clean v0 baseline. Startup creates the current schema with SQLAlchemy metadata and seeds the open community course. It does not preserve retired notebook/job tables or legacy question enum values.

For future schema changes:

- Write an explicit migration note in `docs/deployment-and-upgrades.md`
- Back up `DATABASE_URL` and `DATA_DIR` before deploying
- Keep enum values stable once real deployments depend on them
- Add a migration script or idempotent upgrade step before removing a column/table that may exist in deployed data
- Run `bash scripts/verify.sh` in an environment with `requirements-dev.txt` installed
