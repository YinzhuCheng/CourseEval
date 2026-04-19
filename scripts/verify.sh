#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"

"${PYTHON_BIN}" -m compileall app runner scripts -q
"${PYTHON_BIN}" -m pytest tests/ -q
# Deployment-oriented env checks (no-op unless VERIFY_DEPLOYMENT=1 or --strict).
"${PYTHON_BIN}" scripts/verify_deployment_env.py
