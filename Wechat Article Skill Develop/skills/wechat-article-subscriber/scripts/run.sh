#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  echo "Python 3.9+ is required" >&2
  exit 1
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "Python 3.9+ is required" >&2
  exit 1
}

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
exec "$PYTHON_BIN" "$SCRIPT_DIR/runtime.py" "$@"
