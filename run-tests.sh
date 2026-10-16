#!/usr/bin/env bash
# Corre la suite desde un venv local (se crea la primera vez).
#   ./run-tests.sh              tests unitarios (sin root)
#   ./run-tests.sh -k drives    cualquier argumento va directo a pytest
# Los tests que necesitan rsync/btrfs/ssh/root salen como SKIPPED, nunca como OK.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
VENV=".venv"
if [ ! -x "${VENV}/bin/pytest" ]; then
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install --quiet --upgrade pip
    "${VENV}/bin/pip" install --quiet -r requirements-dev.txt
fi
exec "${VENV}/bin/python" -m pytest "$@"
