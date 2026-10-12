#!/usr/bin/env bash
# Fetch upstream PwnDoc (pinned release) once and start it with its prebuilt images.
# Idempotent: re-running leaves an existing clone alone and just (re)starts the stack.
set -euo pipefail

REPO_URL="${PWNDOC_REPO:-https://github.com/pwndoc/pwndoc}"
REF="${PWNDOC_REF:-v1.7.0}"   # any upstream tag or branch
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${PWNDOC_DIR:-$ROOT/pwndoc}"
PROJECT="${PWNDOC_PROJECT:-pwndoc}"   # compose project name (isolates containers/volumes)

command -v git >/dev/null    || { echo "git is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "the docker compose plugin is required" >&2; exit 1; }

if [ -d "$TARGET/.git" ]; then
  echo "Upstream PwnDoc already present at $TARGET (leaving it as is)."
else
  echo "Cloning $REPO_URL ($REF) into $TARGET ..."
  git clone --depth 1 --branch "$REF" "$REPO_URL" "$TARGET"
fi

# Upstream splits its compose setup: the base file has no images or ports, the prod overlay adds
# the prebuilt ghcr.io images and publishes ${APP_PORT:-8443}. This is what `./pwndoc-cli up` runs.
echo "Starting PwnDoc ..."
(cd "$TARGET" && docker compose -f docker-compose.yml -f docker-compose.prod.yml -p "$PROJECT" up -d)

# start_all.sh sets PWNDOC_QUIET=1 and prints its own summary.
[ "${PWNDOC_QUIET:-0}" = "1" ] && exit 0
PORT="${APP_PORT:-8443}"; PORT="${PORT##*:}"

cat <<MSG

PwnDoc is starting on https://localhost:${PORT} (self-signed certificate).
First run: open it once; PwnDoc shows a first-run page to create the initial administrator.

NOTE: upstream publishes that port on all interfaces, so PwnDoc stays directly reachable from the
LAN next to the AI gateway. To keep it private, set APP_PORT=127.0.0.1:8443 before running this
script (then run the gateway on the same host, not in Docker).

Next, start the AI gateway so it is reachable from your LAN:

  pip install -e '.[rag,https]'      # extras optional
  pwndoc-ai serve --host 0.0.0.0 --port 8080 --https

Then open https://<this-machine-ip>:8080 and log in with your PwnDoc credentials.
MSG
