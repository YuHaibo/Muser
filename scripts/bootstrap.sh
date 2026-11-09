#!/bin/sh
set -eu
MUSER_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$MUSER_PROJECT_DIR"
if [ ! -x .venv/bin/python ]; then uv venv .venv --python 3.11; fi
uv pip install --python .venv/bin/python pytest cryptography==50.0.2 cffi==2.1.1 pillow==13.0.0
# websockets/sounddevice are supplied in vendor/python, so runtime packaging is offline.
