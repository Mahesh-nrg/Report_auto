#!/usr/bin/env bash
# Install/update the ParadigmIT VAPT PwnDoc template. Default is --dry-run.
# Usage: install_pwndoc_template.sh [--dry-run|--apply] [--update] [--stop-services]
#                                   [--template FILE.docx] [--name 'ParadigmIT VAPT']
# Env: DOCKER (docker command, default "docker"), PYTHON, PWNDOC_BACKEND_DIR.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-$HERE/../../.venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" -I "$HERE/pwndoc_installer.py" install "$@"
