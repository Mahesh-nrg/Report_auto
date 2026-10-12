# pwndoc-ai

An AI report assistant for [PwnDoc](https://github.com/pwndoc/pwndoc). It adds an
**✨ Update with AI** button to the audit pages of a stock, unmodified PwnDoc and an
**⚙ AI Settings** panel (LLM endpoint, key and model) for administrators.

PwnDoc itself runs unchanged in Docker. A small gateway (FastAPI, bound to `0.0.0.0` so the whole
LAN can use it) sits in front of it, proxies every request, and injects the AI controls into the
PwnDoc pages. It talks to PwnDoc through its
[REST API](https://pwndoc.github.io/pwndoc/api/) using the permissions of the user who is signed in.
No PwnDoc credentials are stored.

> No PwnDoc fork, no rebuilt front end, no report data in this repository.

## Quick start

Requirements: Python 3.10+, Docker with the compose plugin (for PwnDoc), an API key for any
OpenAI-compatible LLM.

### One command

```bash
./start_all.sh            # also: ./start_all.sh status | stop
```

It clones and starts upstream PwnDoc (bound to `127.0.0.1`), creates the administrator on a fresh
install, installs and starts the gateway over HTTPS, and prints the **port to open**, the
**address** and the **login** to hand to users. The default login is `admin` / `Admin@12345`
(a shared default: change it in PwnDoc > Profile after the first sign-in). The script can only set
the password on a fresh PwnDoc; if users already exist it verifies the login and warns instead.

| Variable | Default | Purpose |
|---|---|---|
| `PWNDOC_ADMIN_USER` / `PWNDOC_ADMIN_PASSWORD` | `admin` / `Admin@12345` | First-run administrator (needs upper, lower, digit, 8+ chars) |
| `GATEWAY_PORT` | `8080` (next free port if busy) | The one port users need; open it in your firewall |
| `PWNDOC_PORT` | `8443` (next free port if busy) | PwnDoc's own port, kept on `127.0.0.1` |
| `GATEWAY_MODE` | `https` | `http` serves plain HTTP (trusted networks only) |
| `PWNDOC_PUBLIC` | `0` | `1` publishes PwnDoc's port on all interfaces too |
| `WITH_RAG` | `0` | `1` installs the local-documents extra |

### Step by step

```bash
# 1. Fetch and start upstream PwnDoc (clones a pinned release into ./pwndoc, prebuilt images)
./scripts/setup_pwndoc.sh

# 2. Install and start the gateway, reachable from your LAN
python -m venv .venv && . .venv/bin/activate
pip install -e '.[rag,https]'        # extras are optional, see below
pwndoc-ai serve --host 0.0.0.0 --port 8080 --https
```

3. Open `https://<this-machine-ip>:8080` (accept the self-signed certificate warning) and log in
   with your normal PwnDoc credentials.
4. A PwnDoc administrator opens **⚙ AI Settings**, enters the LLM base URL, API key and model, and
   uses **Test connection**.
5. Open any audit and click **✨ Update with AI**.

`serve` refuses to listen on a non-loopback address over plain HTTP unless you pass
`--insecure-http` (or set `PWNDOC_AI_ALLOW_HTTP=1`), because logins and the API key would then
cross the network in clear text. Use that only on a network you trust.

Already have PwnDoc running elsewhere? Set `PWNDOC_URL` to its address (see
[Configuration](#configuration)). Run `pwndoc-ai --help` for all options.

### Docker

```bash
cp .env.example .env        # set LLM_*, and PWNDOC_URL if PwnDoc is not on this Docker host
docker compose up -d --build        # gateway on :8080
# EXTRAS=rag,https docker compose up -d --build   # include optional features
```

The compose file only starts the gateway. By default it reaches PwnDoc on the Docker host through
`https://host.docker.internal:8443`; inside a container `localhost` is the container itself, so do
not use it for `PWNDOC_URL`. The stock self-signed PwnDoc certificate needs
`PWNDOC_VERIFY_TLS=false` there (already set in `.env.example`). The compose service acknowledges
plain HTTP with `PWNDOC_AI_ALLOW_HTTP=1`; to serve HTTPS instead, build with `EXTRAS=https` and
override the command with `["pwndoc-ai", "serve", "--host", "0.0.0.0", "--port", "8080", "--https"]`.
Settings, backups and the RAG index live in the `gateway-data` volume.

Note that upstream PwnDoc publishes its own port (`APP_PORT`, default `8443`) on all interfaces, so
it remains reachable from the LAN next to the gateway. `setup_pwndoc.sh` explains how to keep it
private.

## Using it

The button works on whichever page you are on and picks up the audit from the URL automatically.

1. Click **✨ Update with AI** on an audit (or on a single finding).
2. Choose the scope (this finding, selected findings or the whole audit), the fields to update, the
   mode (**Improve existing text** or **Only fill empty fields**) and whether to apply automatically
   (off by default). Internet search and local documents are global options an administrator sets in
   AI Settings.
3. The AI runs as a background job and shows a **preview** of every proposed change.
4. Select the changes you want and **Apply**.
5. Open pages normally refresh by themselves; use **Reload page** if they do not.

The AI rewrites title, description, observation, PoC, scope, remediation and references, and fills
the audit's custom fields (CWE, CVSS score, severity, ...). It may only reuse screenshots
(`<img>` tags) that already exist in the finding: they are always preserved, and the model cannot add
new or external images. Output is sanitised to a small set of formatting tags before it reaches
PwnDoc.

Safety behaviour on apply:

- Right before the first write of a job, the current findings are saved to
  `<data dir>/backups/<audit id>/<UTC timestamp>_<job id>.json`.
- If someone edited a finding in PwnDoc after the preview was made, that finding is **not**
  overwritten; it is marked failed with "changed in PwnDoc since the proposal" and you can re-run it.
- PwnDoc sessions are short-lived. If yours expires during an automatic run, remaining proposals
  stay available to apply manually after you sign in again.
- Each user can run one job at a time, and the gateway runs at most four jobs at once.

## Interactive CLI (JSON in, JSON out, upload)

The same engine is available without the web UI:

```bash
pwndoc-ai refine
```

The wizard asks, step by step, where the findings come from (**a PwnDoc audit** you pick from a
numbered list, or **an existing JSON file**), where to write the polished JSON, which fields and mode
to use, and finally whether (and to which audit) to upload the result. Runs are resumable: findings
already present in the output file are skipped. Input files in UTF-8, UTF-16 or cp1252 are accepted;
output is UTF-8 with private (`0600`) permissions.

Every prompt can also be given as a flag, so a run can be fully non-interactive:

```bash
pwndoc-ai refine --source file --input findings.json --output findings_refined.json \
  --fields description,remediation --mode fill_missing --no-web --no-rag \
  --upload --target-audit <audit-id> --username alice -y
```

| Command | Purpose |
|---|---|
| `pwndoc-ai serve [--host] [--port] [--https] [--insecure-http]` | Run the LAN gateway |
| `pwndoc-ai refine [...]` | The wizard above (`--source {audit,file}`, `--audit`, `--input`, `--output`, `--fields`, `--mode {improve,fill_missing}`, `--web/--no-web`, `--rag/--no-rag`, `--upload/--no-upload`, `--target-audit`, `--username`, `--totp`, `-y`) |
| `pwndoc-ai upload --file F [--audit ID]` | Upload a refined JSON file; findings whose `_id` is not in the audit are skipped and reported |
| `pwndoc-ai config [--set KEY=VALUE]` | Show or change the settings shared with the web UI (`--set llm_api_key=-` prompts for the key so it stays out of shell history) |
| `pwndoc-ai export --audit ID --out report.docx` | Download an audit's DOCX report |

Global flags: `-v/--verbose`, `--data-dir`. The PwnDoc password is read from `PWNDOC_PASSWORD` or a
prompt, never from a flag. Exit codes: `0` ok, `1` error, `2` input closed, `3` some findings failed
or an upload was partial, `130` interrupted. When the source is an audit, the pulled findings are
saved to `<data dir>/exports/audit_<id>_findings.json` (they are your untouched originals), and a
`tokens_used_<timestamp>.json` usage report is written next to the output file. The CLI upload path
does not create a backup.

## Configuration

Settings are edited in **⚙ AI Settings** and saved to `<data dir>/config.json` (mode `0600`). A
value supplied through an environment variable always wins, is shown as the effective value, and is
never written to disk.

| Setting | Env var | Default | Notes |
|---|---|---|---|
| `pwndoc_url` | `PWNDOC_URL` | `https://localhost:8443` | PwnDoc front end the gateway proxies to. Not editable from the browser |
| `verify_tls` | `PWNDOC_VERIFY_TLS` | `true` | Verify PwnDoc's certificate. A loopback upstream (`localhost`, `127.0.0.1`) is always accepted; for a remote host with the stock self-signed certificate set `false`. Not editable from the browser |
| `llm_base_url` | `LLM_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible API. Changing it from the UI clears the stored key so it cannot be sent to the new host |
| `llm_api_key` | `LLM_API_KEY` | _empty_ | Never returned to the browser |
| `llm_model` | `LLM_MODEL` | `gpt-4o-mini` | |
| `temperature` | | `0.2` | 0 to 2 |
| `max_output_tokens` | | `4096` | |
| `use_web_search` | | `false` | DuckDuckGo lookup for CWE/OWASP context. Off by default: finding titles would be sent to a search engine |
| `web_results` | | `3` | Pages fetched per finding (0 to 10) |
| `use_rag` | | `false` | Use your uploaded local documents |
| `redact_field_labels` | | _empty_ | Custom-field labels left out of the prompt and never modified |
| `extra_instructions` | | _empty_ | House style, language or tone appended to the prompt |
| data directory | `PWNDOC_AI_DATA_DIR` | `data` (relative to the working directory) | `config.json`, `backups/`, `exports/`, `rag/`, `tls/` |

`pwndoc_url` and `verify_tls` are locked in the UI on purpose: anyone able to change them could
redirect every user's session cookie. Change them with `pwndoc-ai config --set pwndoc_url=...` or
the environment.

### Provider examples

| Provider | Base URL | Model example |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| OpenRouter | `https://openrouter.ai/api/v1` | any model slug from the OpenRouter catalogue |
| Ollama (local) | `http://localhost:11434/v1` | `llama3.1` (API key can be any non-empty text) |

The engine asks for structured output through function calling. If a provider rejects tools it
retries once without them and parses a JSON reply instead; newer models that require
`max_completion_tokens` are retried accordingly.

### Context sources

- **Internet search** (off by default): DuckDuckGo results are fetched and reduced to extracted page
  text, truncated to about 5,600 characters per page. Only public hosts are contacted; private,
  loopback and link-local addresses are refused, connections are pinned to the validated address,
  and responses are capped at about 1.5 MB.
- **Local documents (RAG)**: install the `rag` extra (`pip install 'pwndoc-ai[rag]'`), upload PDF,
  DOCX, Markdown, text or JSON files (up to 20 MB each) in AI Settings, and enable `use_rag`. Indexing
  uses ChromaDB's built-in embedder (no PyTorch), which downloads a small model on first use. Bring
  your own material; none is shipped.

The combined context is capped at 12,000 characters per finding and is passed to the model as
untrusted reference text.

## Security notes

- Serve over **HTTPS** (`pwndoc-ai serve --https`, needs the `https` extra) or run only on a network
  you trust. Over plain HTTP, PwnDoc passwords and session cookies cross the LAN unencrypted, and the
  gateway has to relax the cookie `Secure` flag in that mode. Behind your own TLS terminator, set
  `FORWARDED_ALLOW_IPS` so the gateway knows the browser is on HTTPS.
- "Administrator" means a PwnDoc user with the `admin` role. AI Settings, model listing and document
  upload are administrator-only, enforced server-side. Any signed-in PwnDoc user can run AI jobs on
  the shared LLM key, so a rogue account can spend your budget. `GET /__ai/api/status` is
  unauthenticated and only reveals the version and whether an LLM is configured.
- Finding text is sent to the configured LLM provider. Use `redact_field_labels` for sensitive
  custom fields (they are omitted from the prompt; title, description, observation, PoC, scope,
  remediation and references are always sent), or a local model (Ollama) for confidential work.
- Review the preview before applying. Model output is a draft, and web or RAG content can contain
  prompt-injection attempts that the sanitiser and the preview step reduce but cannot eliminate.
- Backups, exports and the settings file are created with private permissions, but they contain
  client data: keep the data directory on an encrypted, access-controlled volume.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design and limitations.

## Development

```bash
pip install -e '.[dev,rag,https,ui]'
pytest --cov=pwndoc_ai
ruff check . && ruff format --check .
```

The browser tests (`tests/e2e`, plus `python tests/ui/smoke_ui.py`) need Playwright and a Chromium
binary; they use `/usr/bin/chromium` unless `CHROMIUM` points elsewhere, and are skipped when it is
missing.

## Credits

Built around [PwnDoc](https://github.com/pwndoc/pwndoc) (AGPL-3.0, separate project; not bundled
here) and its [REST API](https://pwndoc.github.io/pwndoc/api/). Check upstream for its own
AI-related features before deploying this alongside it.

## License

[MIT](LICENSE)
