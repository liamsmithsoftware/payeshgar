#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ ! -d ".venv" ]; then
    "$PYTHON_BIN" -m venv .venv
fi
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install --prefer-binary -r requirements.txt
./.venv/bin/python -m playwright install --only-shell chromium || true
exec ./.venv/bin/python run.py "$@"
