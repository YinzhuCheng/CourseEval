#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"

"${PYTHON_BIN}" -m compileall app runner -q
"${PYTHON_BIN}" -m pytest tests/ -q
