#!/usr/bin/env bash
# Start everything needed for AI-assisted reporting:
#   1. upstream PwnDoc (Docker: MongoDB + backend + front end, from github.com/pwndoc/pwndoc)
#   2. the PwnDoc admin account (created on first run)
#   3. the pwndoc-ai gateway (LAN-facing web UI with the "Update with AI" button)
# then prints the ports to open and the login to hand out.
#
#   ./start_all.sh            start (idempotent: safe to re-run)
#   ./start_all.sh status     show what is running
#   ./start_all.sh stop       stop the gateway and PwnDoc (data is kept)
#
# Options (environment variables):
#   PWNDOC_ADMIN_USER       admin username            (default: admin)
#   PWNDOC_ADMIN_PASSWORD   admin password            (default: Admin@12345)
#   GATEWAY_PORT            gateway port              (default: 8080, next free port if busy)
#   PWNDOC_PORT             PwnDoc host port          (default: 8443, next free port if busy)
#   GATEWAY_MODE            https | http              (default: https, self-signed certificate)
#   PWNDOC_PUBLIC           1 = also expose PwnDoc itself on the LAN (default: 0, localhost only)
#   WITH_RAG                1 = install the local-document RAG extra (large download)
#   WITH_PDFIMPORT          1 = install the optional pdf2docx PDF engine (PyMuPDF is AGPL-3.0)
#   (fonts for Word/PDF output: run 'sudo scripts/install_fonts.sh --yes' once, see docs/FONTS.md)
#   PWNDOC_REF              upstream PwnDoc tag/branch (default: v1.7.0)
#   LLM_API_KEY, LLM_BASE_URL, LLM_MODEL   preset the LLM instead of using AI Settings
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

RUN_DIR="$ROOT/.run"
LOG_DIR="$ROOT/logs"
DATA_DIR="${PWNDOC_AI_DATA_DIR:-$ROOT/data}"
PWNDOC_DIR="${PWNDOC_DIR:-$ROOT/pwndoc}"
PROJECT="${PWNDOC_PROJECT:-pwndoc}"
VENV="$ROOT/.venv"

ADMIN_USER="${PWNDOC_ADMIN_USER:-admin}"
ADMIN_PASSWORD="${PWNDOC_ADMIN_PASSWORD:-Admin@12345}"
GATEWAY_MODE="${GATEWAY_MODE:-https}"
PWNDOC_PUBLIC="${PWNDOC_PUBLIC:-0}"
WITH_RAG="${WITH_RAG:-0}"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.prod.yml)

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# -- helpers ---------------------------------------------------------------------------------
port_busy() { [ -n "$(ss -ltnH "sport = :$1" 2>/dev/null)" ]; }

# pick_port <preferred> <explicit:0|1> <label>: use the preferred port, or the next free one.
pick_port() {
  local port="$1" explicit="$2" label="$3" limit=$(($1 + 30))
  while port_busy "$port"; do
    [ "$explicit" = 1 ] && die "$label port $port is already in use (set a different one)."
    port=$((port + 1))
    [ "$port" -le "$limit" ] || die "no free port found for $label near $1."
  done
  echo "$port"
}

# Docker refuses to pull when ~/.docker/config.json names a credential helper that is not
# installed (a common leftover of Docker Desktop). Public images need no login, so use a
# throwaway config in that case.
fix_docker_credentials() {
  local cfg="${DOCKER_CONFIG:-$HOME/.docker}/config.json" helper
  [ -f "$cfg" ] || return 0
  helper="$(python3 - "$cfg" <<'PY' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("credsStore", ""))
except Exception:
    pass
PY
)"
  if [ -n "$helper" ] && ! command -v "docker-credential-$helper" >/dev/null 2>&1; then
    mkdir -p "$RUN_DIR/docker-config"
    echo '{}' > "$RUN_DIR/docker-config/config.json"
    export DOCKER_CONFIG="$RUN_DIR/docker-config"
    info "Docker credential helper '$helper' is not installed; using an anonymous Docker config."
  fi
}

compose() { (cd "$PWNDOC_DIR" && docker compose "${COMPOSE_FILES[@]}" -p "$PROJECT" "$@"); }

# pwndoc_api <METHOD> <PATH>   (JSON body in $API_BODY) -> prints "<http status>\t<body>"
pwndoc_api() {
  PWNDOC_BASE="https://127.0.0.1:$PWNDOC_PORT" python3 - "$@" <<'PY'
import os, ssl, sys, urllib.error, urllib.request

method, path = sys.argv[1:3]
body = os.environ.get("API_BODY")
request = urllib.request.Request(
    os.environ["PWNDOC_BASE"] + path,
    data=body.encode() if body else None,
    method=method,
    headers={"Content-Type": "application/json"},
)
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE  # PwnDoc's own self-signed certificate, on loopback only
try:
    with urllib.request.urlopen(request, context=context, timeout=15) as response:
        print(f"{response.status}\t{response.read().decode(errors='replace')}")
except urllib.error.HTTPError as error:
    print(f"{error.code}\t{error.read().decode(errors='replace')}")
except Exception:
    print("000\t")
PY
}

lan_addresses() {
  ip -4 -o addr show scope global 2>/dev/null \
    | awk '$2 !~ /^(docker|br-|veth|virbr|lo)/ {split($4, a, "/"); print a[1]}'
}

gateway_pid() {
  local pid
  pid="$(cat "$RUN_DIR/gateway.pid" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null \
     && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "pwndoc-ai"; then
    echo "$pid"
  fi
}

stop_gateway() {
  local pid
  pid="$(gateway_pid)"
  if [ -n "$pid" ]; then
    info "Stopping gateway (pid $pid) ..."
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$RUN_DIR/gateway.pid"
}

# -- commands --------------------------------------------------------------------------------
cmd_stop() {
  fix_docker_credentials
  stop_gateway
  if [ -d "$PWNDOC_DIR" ]; then
    info "Stopping PwnDoc containers (data is kept) ..."
    compose stop
  fi
  info "Stopped. Start again with ./start_all.sh"
}

cmd_status() {
  fix_docker_credentials
  local pid port scheme
  pid="$(gateway_pid)"
  port="$(cat "$RUN_DIR/gateway.port" 2>/dev/null || echo "?")"
  scheme="$(cat "$RUN_DIR/gateway.scheme" 2>/dev/null || echo "?")"
  if [ -n "$pid" ]; then echo "gateway: running (pid $pid) on $scheme port $port"; else echo "gateway: stopped"; fi
  if [ -d "$PWNDOC_DIR" ]; then compose ps; else echo "pwndoc: not installed yet"; fi
}

preflight() {
  for tool in git docker curl python3 ss ip; do
    command -v "$tool" >/dev/null || die "$tool is required but not installed."
  done
  docker compose version >/dev/null 2>&1 || die "the docker compose plugin is required."
  docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running, and are you in the docker group?)"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 or newer is required."
  case "$GATEWAY_MODE" in https|http) ;; *) die "GATEWAY_MODE must be https or http." ;; esac
  [[ "$ADMIN_PASSWORD" =~ [0-9] && "$ADMIN_PASSWORD" =~ [a-z] && "$ADMIN_PASSWORD" =~ [A-Z] && ${#ADMIN_PASSWORD} -ge 8 ]] \
    || die "PwnDoc requires a password of 8+ characters with a lowercase letter, an uppercase letter and a digit."
}

start_pwndoc() {
  local own_running=""
  if [ -d "$PWNDOC_DIR/.git" ]; then own_running="$(compose ps -q 2>/dev/null || true)"; fi

  if [ -f "$RUN_DIR/pwndoc.port" ] && [ -n "$own_running" ] && [ -z "${PWNDOC_PORT:-}" ]; then
    PWNDOC_PORT="$(cat "$RUN_DIR/pwndoc.port")"        # keep the port our own stack already uses
  elif [ -n "$own_running" ] && port_busy "${PWNDOC_PORT:-8443}"; then
    PWNDOC_PORT="${PWNDOC_PORT:-8443}"                 # our own containers hold it
  else
    PWNDOC_PORT="$(pick_port "${PWNDOC_PORT:-8443}" "$([ -n "${PWNDOC_PORT:-}" ] && echo 1 || echo 0)" PwnDoc)"
  fi
  echo "$PWNDOC_PORT" > "$RUN_DIR/pwndoc.port"

  local app_port="127.0.0.1:$PWNDOC_PORT"
  [ "$PWNDOC_PUBLIC" = 1 ] && app_port="$PWNDOC_PORT"

  info "Starting PwnDoc (Docker, first run downloads the images) ..."
  APP_PORT="$app_port" PWNDOC_QUIET=1 PWNDOC_PROJECT="$PROJECT" PWNDOC_DIR="$PWNDOC_DIR" \
    PWNDOC_REF="${PWNDOC_REF:-v1.7.0}" "$ROOT/scripts/setup_pwndoc.sh"

  info "Waiting for PwnDoc to answer on port $PWNDOC_PORT ..."
  local out
  for _ in $(seq 1 120); do
    out="$(API_BODY= pwndoc_api GET /api/users/init)"
    [ "${out%%$'\t'*}" = 200 ] && return 0
    sleep 2
  done
  compose logs --tail 30 || true
  die "PwnDoc did not become ready within 4 minutes (see the logs above)."
}

ensure_admin() {
  local out status body
  out="$(API_BODY= pwndoc_api GET /api/users/init)"
  status="${out%%$'\t'*}"; body="${out#*$'\t'}"
  [ "$status" = 200 ] || die "PwnDoc init check failed (HTTP $status)."

  if [[ "$body" == *'"datas":true'* ]]; then
    info "First run: creating the PwnDoc administrator '$ADMIN_USER' ..."
    out="$(API_BODY="$(PW="$ADMIN_PASSWORD" US="$ADMIN_USER" python3 -c \
      'import json,os; print(json.dumps({"username":os.environ["US"],"password":os.environ["PW"],"firstname":"Admin","lastname":"User"}))')" \
      pwndoc_api POST /api/users/init)"
    [ "${out%%$'\t'*}" = 201 ] || die "could not create the admin account: ${out#*$'\t'}"
    CREDS_STATE="created"
    return 0
  fi

  out="$(API_BODY="$(PW="$ADMIN_PASSWORD" US="$ADMIN_USER" python3 -c \
    'import json,os; print(json.dumps({"username":os.environ["US"],"password":os.environ["PW"],"totpToken":""}))')" \
    pwndoc_api POST /api/users/token)"
  if [ "${out%%$'\t'*}" = 200 ]; then
    CREDS_STATE="verified"
  else
    CREDS_STATE="not-applied"
    warn "PwnDoc already has users and '$ADMIN_USER' / the configured password do NOT log in."
    warn "Your existing password is unchanged. Use your real credentials, or reset the data (see README)."
  fi
}

ensure_gateway_install() {
  [ -d "$VENV" ] || { info "Creating Python virtual environment ..."; python3 -m venv "$VENV"; }
  local extras="https" probe="import cryptography"
  if [ "$WITH_RAG" = 1 ]; then extras="rag,https"; probe="import cryptography, chromadb"; fi
  if [ "${WITH_PDFIMPORT:-0}" = 1 ]; then extras="$extras,pdfimport"; probe="$probe, pdf2docx, fitz"; fi
  if [ ! -x "$VENV/bin/pwndoc-ai" ] || ! "$VENV/bin/python" -c "$probe" 2>/dev/null; then
    info "Installing the gateway (extras: $extras) ..."
    "$VENV/bin/pip" install -q --disable-pip-version-check -e "${ROOT}[${extras}]"
  fi
}

start_gateway() {
  stop_gateway
  local explicit=0
  [ -n "${GATEWAY_PORT:-}" ] && explicit=1
  GATEWAY_PORT="$(pick_port "${GATEWAY_PORT:-8080}" "$explicit" Gateway)"
  [ "$explicit" = 0 ] && [ "$GATEWAY_PORT" != 8080 ] && warn "port 8080 is busy, using $GATEWAY_PORT instead."

  mkdir -p "$LOG_DIR" "$DATA_DIR"
  local mode_flag="--https"
  [ "$GATEWAY_MODE" = http ] && mode_flag="--insecure-http"

  info "Starting the AI gateway on port $GATEWAY_PORT ($GATEWAY_MODE) ..."
  PWNDOC_URL="${PWNDOC_URL:-https://127.0.0.1:$PWNDOC_PORT}" \
  PWNDOC_AI_DATA_DIR="$DATA_DIR" \
    nohup "$VENV/bin/pwndoc-ai" serve --host 0.0.0.0 --port "$GATEWAY_PORT" "$mode_flag" \
      >> "$LOG_DIR/gateway.log" 2>&1 < /dev/null &
  echo $! > "$RUN_DIR/gateway.pid"
  disown 2>/dev/null || true
  echo "$GATEWAY_PORT" > "$RUN_DIR/gateway.port"
  echo "$GATEWAY_MODE" > "$RUN_DIR/gateway.scheme"

  for _ in $(seq 1 40); do
    if curl -ks --max-time 3 "$GATEWAY_MODE://127.0.0.1:$GATEWAY_PORT/__ai/api/status" >/dev/null 2>&1; then
      return 0
    fi
    kill -0 "$(cat "$RUN_DIR/gateway.pid")" 2>/dev/null || break
    sleep 0.5
  done
  tail -n 20 "$LOG_DIR/gateway.log" >&2 || true
  die "the gateway did not start (log above, full log: $LOG_DIR/gateway.log)."
}

print_summary() {
  local ips url_lines="" ip
  ips="$(lan_addresses)"
  for ip in $ips; do url_lines+="      $GATEWAY_MODE://$ip:$GATEWAY_PORT"$'\n'; done
  [ -n "$url_lines" ] || url_lines="      $GATEWAY_MODE://<this-machine-ip>:$GATEWAY_PORT"$'\n'

  local line
  line="$(printf '%*s' 72 '' | tr ' ' '=')"
  cat <<SUMMARY

$line
 AI report automation is running
$line

 1) PORT TO OPEN (firewall / security group), TCP, inbound from your LAN:

      $GATEWAY_PORT/tcp   AI gateway + PwnDoc UI   <- the only port users need
SUMMARY
  if [ "$PWNDOC_PUBLIC" = 1 ]; then
    echo "      $PWNDOC_PORT/tcp   PwnDoc directly (bypasses the AI button; PWNDOC_PUBLIC=1)"
  else
    echo "      (PwnDoc itself listens on 127.0.0.1:$PWNDOC_PORT only. Do NOT open that port.)"
  fi
  [ "$GATEWAY_MODE" = http ] && echo "      WARNING: plain HTTP. Logins and the API key are visible on the network."
  cat <<SUMMARY
      Example (ufw):  sudo ufw allow from <your-lan-cidr> to any port $GATEWAY_PORT proto tcp

 2) ADDRESS TO GIVE USERS:

$url_lines$([ "$GATEWAY_MODE" = https ] && echo "      (self-signed certificate: the browser shows a warning once, choose Advanced > Continue)")

 3) LOGIN TO GIVE USERS:

      Username:  $ADMIN_USER
      Password:  $ADMIN_PASSWORD
SUMMARY
  case "${CREDS_STATE:-}" in
    created)     echo "      (account created just now; this is a shared default, change it in PwnDoc > Profile)" ;;
    verified)    echo "      (verified: these credentials log in)" ;;
    not-applied) echo "      !! NOT APPLIED: PwnDoc already had users. Use your existing credentials instead." ;;
  esac
  cat <<SUMMARY

 4) ONE-TIME SETUP (administrator): log in, open the purple "AI Settings" button, enter the LLM
    base URL, API key and model, then "Test connection". Then open any audit and click
    "Update with AI".

 Logs:    $LOG_DIR/gateway.log        Data:  $DATA_DIR
 Status:  ./start_all.sh status       Stop:  ./start_all.sh stop
$line
SUMMARY
}

# -- main ------------------------------------------------------------------------------------
mkdir -p "$RUN_DIR"
case "${1:-start}" in
  start)
    preflight
    fix_docker_credentials
    start_pwndoc
    ensure_admin
    ensure_gateway_install
    start_gateway
    print_summary
    ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  -h|--help|help) sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$1' (use: start | status | stop)" ;;
esac
