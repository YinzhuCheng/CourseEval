# CourseEval

CourseEval is a lightweight course-assignment evaluation platform for teaching teams.

The current product direction is:

- **Native Python evaluation** for executable programming questions using `.py` submissions
- **File / LLM-reviewed evaluation** for `.pdf`, `.txt`, `.tex`, and `.ipynb` submissions
- **Teacher-confirmed grading** for workflows where automatic suggestions should not directly become final grades

The old standalone Notebook execution workflow has been retired. Existing `/dashboard` and `/jobs/*` links now redirect users to the in-product Python runtime help page so they can move to the supported flows.

## What the system supports

### Student workflows

- Register and sign in
- Join a course with a join code
- View courses, assignments, questions, and submission history
- Submit:
  - `.py` files for native Python code questions
  - `.pdf` files for PDF / LLM-reviewed questions
  - `.txt`, `.tex`, or `.ipynb` files for formatted-text / LLM-reviewed questions
- View evaluation progress, feedback, and downloadable artifacts
- Read built-in Python runtime help inside the product UI

### Teacher workflows

- Create and manage courses
- Share course join codes with students
- Create assignments
- Create question types for:
  - native Python code evaluation
  - PDF file / LLM-reviewed evaluation
  - formatted text or `.ipynb` / LLM-reviewed evaluation
- Configure Python test cases, scoring rules, and submission limits
- Review submissions and confirm final grades

### Admin workflows

- Manage user roles
- Manage runtime image records
- Manage LLM configuration records
- Review system overview data

## Supported Python runtime

The default native Python evaluation runtime currently provides:

- **Python** `3.12`

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
- The default runtime image does **not** preinstall deep-learning frameworks such as `torch`, `tensorflow`, `jax`, `paddle`, `mxnet`, or `transformers`
- Students can also view the same information inside the product at `/student/help/python-runtime`

## Submission model

### 1. Native Python code questions

Use this when students should submit executable `.py` files and receive test-based automatic evaluation.

Typical flow:

1. Teacher creates a **Python code** question
2. Teacher configures visible and hidden test cases
3. Student uploads a `.py` file
4. The worker runs the code in an isolated Docker container
5. The system stores structured evaluation results and feedback
6. The final grade snapshot is updated

### 2. File / LLM-reviewed questions

Use this when students should submit a report, analysis, explanation, or notebook-style artifact.

Supported submission file types:

- `.pdf`
- `.txt`
- `.tex`
- `.ipynb`

For `.ipynb`:

- `.ipynb` is supported through the **file / LLM-reviewed** route
- The old standalone Notebook execution page is **not** part of the active workflow anymore
- Teachers can require notebooks to already contain executed outputs before upload

Typical flow:

1. Teacher creates a **PDF** or **formatted text / ipynb** question
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
- **Docker** for isolated execution of native Python code questions
- **Bootstrap 5** for the UI

## Important modules

- `app/main.py`: FastAPI entrypoint
- `app/routes/auth.py`: registration, login, locale switching
- `app/routes/student.py`: student pages and submission entrypoints
- `app/routes/teacher.py`: teacher course, assignment, question, and grading pages
- `app/routes/admin.py`: admin pages and runtime / LLM configuration pages
- `app/routes/jobs.py`: compatibility redirects for the retired notebook runner routes
- `app/services/submissions.py`: submission orchestration and background evaluation logic
- `app/services/courses.py`: course, assignment, and question helpers
- `app/runtime_support.py`: canonical Python version and package support matrix
- `runner/execute_python_code.py`: helper script used inside the Python runner container
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
│   │   ├── jobs.py
│   │   ├── student.py
│   │   └── teacher.py
│   ├── services/
│   │   ├── courses.py
│   │   ├── llm.py
│   │   ├── permissions.py
│   │   └── submissions.py
│   ├── static/
│   │   └── style.css
│   └── templates/
├── data/
├── runner/
│   ├── Dockerfile
│   └── execute_python_code.py
├── scripts/
├── tests/
├── worker.py
├── requirements.txt
└── README.md
```

## Local development

### 1. Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

### 2. Prepare environment file

```bash
cp .env.example .env
```

Update `SECRET_KEY` before real deployment.

### 3. Initialize the database

```bash
python scripts/init_db.py
```

### 4. Start Redis

If Redis is already installed:

```bash
redis-server --save "" --appendonly no
```

Or with Docker:

```bash
docker run --rm -p 6379:6379 redis:7-alpine
```

### 5. Build the default runner image

```bash
docker build -t courseeval-python-runner:latest runner
```

This image contains the default native Python runtime used for `.py` evaluation.

### 6. Start the web service

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 7. Start the worker

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

## Email verification deployment notes

Public registration now uses a two-step flow:

1. The user submits the registration form
2. The system creates a pending account with `email_verified = false`
3. The system sends a verification email containing `/verify-email?token=...`
4. The user clicks the email link
5. The account becomes active and can sign in

### Required environment variables

Set these values in `.env` for production:

```bash
APP_BASE_URL=https://your-domain.example.com
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
- Configure a working SMTP account before enabling public registration in production
- Use HTTPS in front of the FastAPI app so email verification links and session cookies travel securely
- Make sure the email sender domain and mailbox are allowed by your mail provider
- If you run multiple app instances, they must share the same database so verification tokens stay valid across nodes
- Keep `SECRET_KEY` stable across restarts so session handling remains consistent

### Behavior when SMTP is not configured

If SMTP is missing or delivery fails, the account is still created in a pending state, but the user cannot complete registration until email sending works and the verification email is resent.

## Data model notes

Main teaching-domain tables include:

- `courses`
- `course_members`
- `assignments`
- `questions`
- `python_code_question_configs`
- `file_question_configs`
- `short_answer_question_configs`
- `runtime_images`
- `llm_configs`
- `submissions`
- `evaluation_tasks`
- `evaluation_results`
- `feedback`
- `final_grade_snapshots`

Legacy compatibility tables are still present in the schema:

- `notebooks`
- `jobs`
- `job_outputs`

Those tables remain only for backward compatibility and route migration. They are no longer the primary product workflow.

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

### Python evaluation fails immediately with Docker-related errors

Check:

- Docker is installed
- The current user can run `docker`
- The runner image exists:

```bash
docker images | rg courseeval-python-runner
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

