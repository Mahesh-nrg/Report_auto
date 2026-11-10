#!/usr/bin/env bash
# Interactive installer for pwndoc-ai: writes .env (LLM / embeddings settings), then runs
# start_all.sh to bring up the full stack (upstream PwnDoc in Docker + the AI gateway).
#
#   ./install.sh                 interactive: asks questions, writes .env, then starts everything
#   ./install.sh --yes           non-interactive: use existing .env / environment, no prompts
#   ./install.sh --reconfigure   force the questions again even if .env already exists
#   ./install.sh --help
#
# Every answer can also be preset as an environment variable (see the prompts below), so this
# can be scripted: e.g. LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=... ./install.sh --yes
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
ENV_FILE="$ROOT/.env"

NONINTERACTIVE=0
FORCE_RECONFIGURE=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes|--non-interactive) NONINTERACTIVE=1 ;;
    --reconfigure) FORCE_RECONFIGURE=1 ;;
    -h|--help)
      sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ask <var-name> <prompt> <default>  -- skips the prompt if $var-name is already set in the
# environment (so this script is scriptable / CI-friendly), or if running --yes.
ask() {
  local var="$1" prompt="$2" default="${3-}" current reply
  current="${!var-}"
  if [ -n "$current" ]; then return 0; fi
  if [ "$NONINTERACTIVE" = 1 ]; then printf -v "$var" '%s' "$default"; return 0; fi
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " reply || true
  else
    read -r -p "$prompt: " reply || true
  fi
  printf -v "$var" '%s' "${reply:-$default}"
}

# ask_secret <var-name> <prompt>  -- like ask(), but does not echo the input.
ask_secret() {
  local var="$1" prompt="$2" current reply
  current="${!var-}"
  if [ -n "$current" ]; then return 0; fi
  if [ "$NONINTERACTIVE" = 1 ]; then printf -v "$var" '%s' ""; return 0; fi
  read -r -s -p "$prompt: " reply || true
  echo
  printf -v "$var" '%s' "$reply"
}

# ask_yn <var-name> <prompt> <default: y|n>  -- sets $var to 1 or 0.
ask_yn() {
  local var="$1" prompt="$2" default="$3" current reply yn
  current="${!var-}"
  if [ -n "$current" ]; then return 0; fi
  if [ "$NONINTERACTIVE" = 1 ]; then
    [ "$default" = y ] && printf -v "$var" 1 || printf -v "$var" 0
    return 0
  fi
  [ "$default" = y ] && yn="Y/n" || yn="y/N"
  read -r -p "$prompt [$yn]: " reply || true
  reply="${reply:-$default}"
  case "$reply" in [Yy]*) printf -v "$var" 1 ;; *) printf -v "$var" 0 ;; esac
}

banner() {
  local line; line="$(printf '%*s' 72 '' | tr ' ' '=')"
  echo
  echo "$line"
  echo " pwndoc-ai installer"
  echo "$line"
  echo
  echo " This installs the full stack: upstream PwnDoc (Docker: MongoDB + backend + front end)"
  echo " and the AI gateway in front of it, then asks for your LLM / embeddings configuration."
  echo
}

preflight() {
  info "Checking prerequisites ..."
  for tool in git docker curl python3; do
    command -v "$tool" >/dev/null || die "$tool is required but not installed."
  done
  docker compose version >/dev/null 2>&1 || die "the docker compose plugin is required."
  docker info >/dev/null 2>&1 \
    || die "cannot talk to the Docker daemon (is it running, and are you in the docker group?)"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 or newer is required."
  info "OK."
}

# -- write a quoted KEY=value line into the .env, only if the value is non-empty -------------
env_lines=()
add_env() { local key="$1" value="$2"; [ -n "$value" ] && env_lines+=("$key=$value"); }

configure() {
  echo
  echo "--- PwnDoc administrator (first run only; existing installs keep their own users) ---"
  ask PWNDOC_ADMIN_USER "Admin username" "admin"
  ask_secret PWNDOC_ADMIN_PASSWORD_INPUT "Admin password (blank = default Admin@12345, change it later)"
  PWNDOC_ADMIN_PASSWORD="${PWNDOC_ADMIN_PASSWORD_INPUT:-${PWNDOC_ADMIN_PASSWORD:-Admin@12345}}"

  echo
  echo "--- LLM (any OpenAI-compatible endpoint: OpenAI, OpenRouter, Ollama, LM Studio, vLLM...) ---"
  echo "    This can also be left blank and set later in the app's AI Settings panel."
  ask LLM_BASE_URL "LLM base URL" "https://api.openai.com/v1"
  ask_secret LLM_API_KEY_INPUT "LLM API key (blank if your endpoint needs none, e.g. local Ollama)"
  LLM_API_KEY="${LLM_API_KEY_INPUT:-${LLM_API_KEY:-}}"
  ask LLM_MODEL "LLM model name" "gpt-4o-mini"
  ask LLM_FALLBACK_MODELS "Fallback model(s), comma-separated (optional)" ""

  echo
  echo "--- Embeddings (used for web-search/RAG context; small model, started on demand) ---"
  echo "    auto = use Ollama if installed, else the built-in ONNX embedder; or force one."
  ask PWNDOC_AI_EMBEDDER "Embedding backend (auto / ollama / onnx)" "auto"
  if [ "$PWNDOC_AI_EMBEDDER" = "ollama" ] || [ "$PWNDOC_AI_EMBEDDER" = "auto" ]; then
    ask PWNDOC_AI_OLLAMA_MODEL "Ollama embedding model" "all-minilm"
    ask PWNDOC_AI_OLLAMA_URL "Existing Ollama server URL (blank = start one automatically)" ""
  fi

  echo
  echo "--- Optional features ---"
  ask_yn WITH_RAG "Enable local-document RAG (adds chromadb, larger install)" n
  ask_yn WITH_PDFIMPORT "Enable PDF import engine (pdf2docx/PyMuPDF, AGPL-3.0)" n
  ask_yn USE_WEB_SEARCH "Allow internet web-search context by default (sends finding titles out)" n

  echo
  echo "--- Network ---"
  ask GATEWAY_MODE "Gateway mode: https (recommended) or http" "https"
  ask GATEWAY_PORT "Gateway port (the one port to open on your LAN/firewall)" "8080"
  ask PWNDOC_PORT "PwnDoc's own port (stays on 127.0.0.1 unless published below)" "8443"
  ask_yn PWNDOC_PUBLIC "Also expose PwnDoc's own port on the LAN (bypasses the AI gateway)" n

  # -- persist to .env (consumed by docker-compose; start_all.sh also exports these for the
  #    venv-based gateway run) -----------------------------------------------------------------
  env_lines=()
  add_env "PWNDOC_VERIFY_TLS" "false"
  add_env "LLM_BASE_URL" "$LLM_BASE_URL"
  add_env "LLM_API_KEY" "$LLM_API_KEY"
  add_env "LLM_MODEL" "$LLM_MODEL"
  if [ -n "${LLM_FALLBACK_MODELS:-}" ]; then add_env "LLM_FALLBACK_MODELS" "$LLM_FALLBACK_MODELS"; fi
  add_env "PWNDOC_AI_EMBEDDER" "$PWNDOC_AI_EMBEDDER"
  if [ -n "${PWNDOC_AI_OLLAMA_MODEL:-}" ]; then add_env "PWNDOC_AI_OLLAMA_MODEL" "$PWNDOC_AI_OLLAMA_MODEL"; fi
  if [ -n "${PWNDOC_AI_OLLAMA_URL:-}" ]; then add_env "PWNDOC_AI_OLLAMA_URL" "$PWNDOC_AI_OLLAMA_URL"; fi
  add_env "PWNDOC_AI_DATA_DIR" "data"

  {
    echo "# Generated by install.sh on $(date -u +%FT%TZ). Edit freely; re-run"
    echo "# './install.sh --reconfigure' to be asked again, or edit values by hand."
    printf '%s\n' "${env_lines[@]}"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  info "Wrote $ENV_FILE"
}

maybe_configure() {
  if [ -f "$ENV_FILE" ] && [ "$FORCE_RECONFIGURE" = 0 ]; then
    info "$ENV_FILE already exists; keeping it (use --reconfigure to be asked again)."
    # still load it so start_all.sh/compose see the same values this run.
    set -a; . "$ENV_FILE"; set +a
    return 0
  fi
  if [ "$NONINTERACTIVE" = 1 ] && [ -f "$ENV_FILE" ]; then
    set -a; . "$ENV_FILE"; set +a
  fi
  configure
  set -a; . "$ENV_FILE"; set +a
}

run_stack() {
  info "Starting PwnDoc (Docker) and the AI gateway ..."
  export PWNDOC_ADMIN_USER PWNDOC_ADMIN_PASSWORD GATEWAY_MODE GATEWAY_PORT PWNDOC_PORT PWNDOC_PUBLIC
  [ "${WITH_RAG:-0}" = 1 ] && export WITH_RAG=1
  [ "${WITH_PDFIMPORT:-0}" = 1 ] && export WITH_PDFIMPORT=1
  export LLM_BASE_URL LLM_API_KEY LLM_MODEL
  "$ROOT/start_all.sh" start
}

main() {
  banner
  preflight
  maybe_configure
  run_stack
  echo
  info "Done. Re-run '$0' any time (idempotent); use './start_all.sh status' or './start_all.sh stop'."
  if [ "${USE_WEB_SEARCH:-0}" = 1 ]; then
    warn "Web search is a per-install default only; turn it on in AI Settings (admin) to take effect."
  fi
}

main
