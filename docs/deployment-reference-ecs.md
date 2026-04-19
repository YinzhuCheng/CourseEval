# ECS deployment reference (modes, toggles, copy-paste)

**Audience:** operators and coding agents preparing a **Linux ECS-style host** (single VM or one node that runs web + worker + Redis + Docker like a small deployment).

**Scope:** This file is **reference text and shell snippets**, not a Terraform/CloudFormation module. It encodes **deployment modes** and **optional capability bundles** so you do not mix “greenfield install”, “upgrade with data”, and “email on/off” ad hoc.

**Related:** Operational contract and persistence notes live in [`deployment-and-upgrades.md`](deployment-and-upgrades.md). Python install layers are documented there and in [`AGENTS.md`](../AGENTS.md).

---

## 1. Deployment target

- **Target:** One ECS server (or equivalent) running **the same `.env`** for:
  - `uvicorn app.main:app` (web)
  - `python worker.py` (RQ workers)
  - **Redis** (reachable at `REDIS_URL` from both web and worker)
  - **Docker** (host daemon) if you use **code questions** (`RUNNER_IMAGE`).

---

## 2. Deployment modes (choose one per rollout)

These two modes are **orthogonal** to optional features (SMTP, invite code, code runner, LLM). Pick the mode first, then apply the toggles in section 3.

### 2.1 Mode A — Clean slate (discard legacy state)

**Intent:** Start from an empty application dataset and empty file artifacts to avoid **stale files, half-migrated trees, naming collisions, or unexplained bugs** after repeated experiments on the same host.

**When to use:**

- First production install on a fresh disk/volume.
- The current `DATA_DIR` / database is **disposable** (no courses you need to keep).
- You suspect corruption or inconsistent manual edits under `DATA_DIR` and want a provably clean tree.

**What it means:**

- The SQLite file (default `DATA_DIR/app.db`) and directories under `DATA_DIR` (`uploads/`, `outputs/`, …) are **removed or replaced** with empty targets **before** starting the new version.
- After startup, the app recreates schema via `init_database()` and seeds the open community course (see `deployment-and-upgrades.md`).

**Risk:** **Irreversible data loss** for everything under the removed paths unless you archived backups elsewhere.

### 2.2 Mode B — Preserve legacy and migrate (upgrade in place)

**Intent:** Deploy a **new application version** while **keeping** existing database and files (courses, submissions, uploads, artifacts).

**When to use:**

- Routine **version upgrades** on a server that already serves users.
- You need continuity of grades, submissions, and LLM/runtime configuration stored in the DB.

**What it means:**

- **Before** switching traffic or starting new code against live data: **stop web and worker** (or otherwise stop writes), then **back up** `DATABASE_URL` target and the whole `DATA_DIR` (see `deployment-and-upgrades.md`).
- Deploy new code and configuration, keep **stable** `SECRET_KEY`, `DATA_DIR`, and `DATABASE_URL` paths unless you intentionally relocate storage with a planned copy.
- Start services and run smoke tests (login, one code submission if applicable, one file/LLM path if applicable).

**Caveat:** This repository does **not** ship Alembic migrations; `create_all()` does not alter existing columns. Breaking schema changes require an explicit migration path documented separately.

---

## 3. Optional capability bundles (compose, do not conflate)

Treat each row as a **bundle**. For example, “email optional” means the **entire SMTP bundle** is absent or disabled—not “only SMTP password is optional” while host/from are missing.

| Bundle | Env / config | If omitted or “off” |
|--------|----------------|---------------------|
| **A. Outbound email** | `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_ADDRESS`, `SMTP_FROM_NAME`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS`, `SMTP_USE_SSL` | Registration may still create users, but **verification / password reset / admin test mail** will not work reliably. Set **`APP_BASE_URL`** anyway if the site is public. |
| **B. Invite-only registration** | `REGISTRATION_INVITE_CODE` (non-empty) | Empty → invite field not required at registration (open registration UX). |
| **C. Code evaluation** | Docker + `RUNNER_IMAGE` (+ build `runner/` image) | Without Docker/image, **code questions** cannot run; file/LLM paths may still work if worker + LLM config exist. |
| **D. File / LLM grading** | Redis + worker + **LLMConfig** rows in DB (not a single `.env` secret) | Without LLM configs, file-LLM questions have nothing to call. |

**Strongly recommended for any non-toy ECS deploy (not “optional” in the sense of product feature, but optional in templates):**

- `SECRET_KEY` — long random, **not** the default placeholder.
- `APP_BASE_URL` — public `https://…` origin when users reach the site over HTTPS (required for **correct email links** when bundle A is on).
- `DATABASE_URL`, `DATA_DIR`, `REDIS_URL` — explicit paths/URLs on ECS even if defaults work locally.

---

## 4. Baseline `.env` fragment (ECS — adjust paths)

Use one file on the server (e.g. `/etc/courseeval.env`) or your orchestrator’s secret injection. **Web and worker must see identical values** for queue names and Redis.

```bash
# --- Core (adjust for your host) ---
export SECRET_KEY='REPLACE_WITH_LONG_RANDOM'
export APP_BASE_URL='https://eval.example.com'
export DATABASE_URL='sqlite:////var/lib/courseeval/app.db'
export DATA_DIR='/var/lib/courseeval/data'
export REDIS_URL='redis://127.0.0.1:6379/0'
export PYTHON_QUEUE_NAME='python-evaluations'
export LLM_QUEUE_PREFIX='llm-evaluations'

# --- Runner (code questions) ---
export RUNNER_IMAGE='courseeval-runner:latest'
export RUNNER_MEMORY_LIMIT='1g'
export RUNNER_CPUS='1'
export EXECUTION_TIMEOUT_SECONDS='300'
export DOCKER_NETWORK_DISABLED='true'

# --- Uploads / PDF ---
export UPLOAD_MAX_BYTES='5242880'
export PDF_REVIEW_MAX_PAGES='8'
```

---

## 5. Optional fragments (copy only what you need)

### 5.1 Bundle A — SMTP enabled (full set)

```bash
export SMTP_HOST='smtp.example.com'
export SMTP_PORT='587'
export SMTP_USERNAME='smtp-user'
export SMTP_PASSWORD='smtp-secret'
export SMTP_FROM_ADDRESS='noreply@example.com'
export SMTP_FROM_NAME='CourseEval'
export SMTP_STARTTLS='true'
export SMTP_USE_SSL='false'
```

### 5.2 Bundle B — Invite-only registration

```bash
export REGISTRATION_INVITE_CODE='your-invite-secret'
```

### 5.3 Bundle A off (explicit “no SMTP” for clarity in scripts)

Leave SMTP variables unset or empty:

```bash
unset SMTP_HOST SMTP_USERNAME SMTP_PASSWORD SMTP_FROM_ADDRESS SMTP_FROM_NAME || true
export SMTP_HOST=''
```

(Application code treats empty host as “no SMTP”; registration flows may still run without deliverable mail.)

---

## 6. Reference shell — Mode A (clean slate) on ECS

**DANGER:** Deletes the default SQLite DB and typical artifact trees under `DATA_DIR`. Run only after confirming **no production data** you need lives there.

```bash
set -euo pipefail

# Stop consumers first (use your process manager: systemd, supervisor, docker compose, etc.)
# sudo systemctl stop courseeval-web courseeval-worker || true

export DATA_DIR="${DATA_DIR:-/var/lib/courseeval/data}"
export DATABASE_URL="${DATABASE_URL:-sqlite:////var/lib/courseeval/app.db}"

# Optional: archive instead of rm -rf (strongly recommended if unsure)
# sudo tar -czf "/backup/courseeval-data-$(date +%Y%m%d%H%M).tgz" "$DATA_DIR" "$(dirname "$(echo "$DATABASE_URL" | sed -n 's#^sqlite:////*##p')")" 2>/dev/null || true

sudo rm -rf "${DATA_DIR}/uploads" "${DATA_DIR}/outputs" || true
sudo rm -f "/var/lib/courseeval/app.db" 2>/dev/null || true
sudo mkdir -p "$DATA_DIR"
sudo chown -R courseeval:courseeval "$DATA_DIR" 2>/dev/null || true

# Deploy new code + install Layer 1 requirements, then start web/worker/redis/docker per your layout.
# After first boot, init_database() creates schema; optionally run: python scripts/init_db.py
```

**Note:** If `DATABASE_URL` is not the default path, delete the **actual** DB file that URL points to instead of the hardcoded path above.

---

## 7. Reference shell — Mode B (migrate / upgrade) on ECS

**SAFE baseline:** backup, then deploy new code, then restart.

```bash
set -euo pipefail
export DATA_DIR="${DATA_DIR:-/var/lib/courseeval/data}"
export DATABASE_URL="${DATABASE_URL:-sqlite:////var/lib/courseeval/app.db}"

# Stop web + worker (same as your production unit files)
# sudo systemctl stop courseeval-web courseeval-worker

sudo mkdir -p "/backup/courseeval"
# Always archive DATA_DIR (uploads, outputs, …).
sudo tar -czf "/backup/courseeval/pre-upgrade-$(date +%Y%m%d%H%M)-data.tgz" "$DATA_DIR"
# If DATABASE_URL is a SQLite file path, archive that file too (set DB_FILE to match your DATABASE_URL).
# Example when DATABASE_URL is sqlite:////var/lib/courseeval/app.db:
# sudo tar -czf "/backup/courseeval/pre-upgrade-$(date +%Y%m%d%H%M)-db.tgz" -C /var/lib/courseeval app.db

# Deploy new application tree + pip install -r requirements.txt
# sudo systemctl start redis || true
# sudo systemctl start courseeval-web courseeval-worker
```

For non-SQLite `DATABASE_URL`, use your DB vendor’s backup tool instead of `tar` on a file path.

---

## 8. Scenario matrix (how to combine modes + bundles)

| Scenario | Mode | Bundle A (SMTP) | Bundle B (invite) | Bundle C (Docker/code) | Bundle D (LLM) |
|----------|------|-----------------|-------------------|--------------------------|------------------|
| Internal demo, no mail | A or B | Off | Optional | Optional | Optional |
| Production with mail + open reg | B (or A first time) | On | Off | On if code questions | On if file-LLM |
| Production with mail + invite | B | On | On | On if code questions | On if file-LLM |
| Air-gapped lab, no outbound SMTP | A or B | Off | On/Off | On/Off | Off or local provider |

**`bash scripts/verify.sh`** validates **application** code (dev install: `requirements-dev.txt`); it does **not** replace rebuilding the **runner image** for bundle C. See `deployment-and-upgrades.md` and `README.md`.

---

## 9. After deploy (both modes)

1. `GET /healthz` through your HTTPS front end.
2. If bundle A on: send registration or use admin SMTP test.
3. If bundle C on: submit a trivial code question once.
4. If bundle D on: submit a small file-LLM question and confirm worker + LLM queue.

---

## 10. Naming this file for agents

When asking an agent to “follow ECS deployment reference”, point to **`docs/deployment-reference-ecs.md`** and specify **Mode A vs B** plus which bundles (A–D) apply.
