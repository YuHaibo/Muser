#!/bin/sh
set -eu
MUSER_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export MUSER_ROOT="$MUSER_PROJECT_DIR"
export MUSER_PYTHON="${MUSER_PYTHON:-$MUSER_PROJECT_DIR/.venv/bin/python}"
exec "$MUSER_PROJECT_DIR/build/desktop/muser" "$@"
