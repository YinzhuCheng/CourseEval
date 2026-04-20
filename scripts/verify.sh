#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"

"${PYTHON_BIN}" -m compileall app runner tests -q
"${PYTHON_BIN}" -m ruff check app runner tests --select F401,F841
"${PYTHON_BIN}" -m pytest tests/ -q
