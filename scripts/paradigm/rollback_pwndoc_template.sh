#!/usr/bin/env bash
# Remove ONLY what install_pwndoc_template.sh created (ids from created.json). Default is --dry-run.
# Usage: rollback_pwndoc_template.sh [--dry-run|--apply] [--name 'ParadigmIT VAPT'] [--backup-dir DIR]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-$HERE/../../.venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" -I "$HERE/pwndoc_installer.py" rollback "$@"
