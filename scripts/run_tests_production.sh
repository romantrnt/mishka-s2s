#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${MISHKA_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
MODE="${1:-full}"
shift || true
PYTHON_BIN="${MISHKA_TEST_PYTHON:-$ROOT/.test-venv-p1-20261007/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "test Python is missing: $PYTHON_BIN; set MISHKA_TEST_PYTHON to an isolated test venv" >&2
  exit 2
fi
exec env MISHKA_TEST_PYTHON="$PYTHON_BIN" "$PYTHON_BIN" scripts/run_regression.py   "$MODE" --environment production "$@"
