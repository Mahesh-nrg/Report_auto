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

> ### ⚠ Change the default password
> `start_all.sh` creates the PwnDoc administrator **`admin` / `Admin@12345`**. This default is public
> (it is in this repository), so treat it as a placeholder:
>
> - **Best:** choose your own before the first run:
>   `PWNDOC_ADMIN_USER=alice PWNDOC_ADMIN_PASSWORD='Str0ng-Passw0rd' ./start_all.sh`
>   (at least 8 characters with an upper-case letter, a lower-case letter and a digit).
> - **Or:** log in once and change it in PwnDoc (user menu, top right > **Profile** > new password).
>   Do this before giving the address to anyone, and never leave the default on a network you do not
>   fully trust.
>
> The script can only set the password on a **fresh** PwnDoc database. If PwnDoc already has users it
> leaves them alone, checks whether the configured login works, and warns if it does not.

### Requirements

| What | Why |
|---|---|
| Linux or macOS host with `bash`, `git`, `curl`, `python3` (3.10+) | run the scripts and the gateway |
| Docker Engine with the **compose plugin** (`docker compose version`) and a running daemon | runs PwnDoc and MongoDB |
| Internet access on first run | clone PwnDoc, pull its images, `pip install` |
| An API key for an OpenAI-compatible LLM (or a local Ollama) | needed only when you use **Update with AI** |

### Install and run

```bash
# 1. Get the code
git clone https://github.com/Mahesh-nrg/Report_auto.git
cd Report_auto

# 2. (Recommended) pick your own admin password, then start everything
export PWNDOC_ADMIN_PASSWORD='Str0ng-Passw0rd'     # skip to use the default Admin@12345
./start_all.sh
```

The first run takes a few minutes (image download). The script then:

1. checks the prerequisites above;
2. clones upstream PwnDoc (pinned release) and starts it with Docker, listening on `127.0.0.1` only;
3. waits until PwnDoc answers and creates the administrator on a fresh install;
4. creates `.venv`, installs the gateway and starts it on `0.0.0.0` over HTTPS;
5. prints a summary with the **port to open**, the **address** and the **login**, for example:

```text
 1) PORT TO OPEN (firewall / security group), TCP, inbound from your LAN:
      8080/tcp   AI gateway + PwnDoc UI   <- the only port users need
 2) ADDRESS TO GIVE USERS:   https://<this-machine-ip>:8080
 3) LOGIN TO GIVE USERS:     Username: admin    Password: <your password>
```

Open only the **gateway port** to your LAN (default `8080`; the script picks the next free port and
tells you if it is busy). PwnDoc's own port (`8443`) stays on `127.0.0.1`, so do not open it.
Example with ufw: `sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp`.

### First-time setup in the browser

1. Open the printed address. The certificate is self-signed, so the browser warns once: choose
   **Advanced > Continue**.
2. Log in with the printed username and password, then **change the password** if you kept the
   default (see the warning above).
3. As an administrator, click the purple **⚙ AI Settings** button, enter the LLM **base URL**, **API
   key** and **model**, and press **Test connection**, then **Save**.
4. Open any audit and click **✨ Update with AI**. Other PwnDoc users log in with their own accounts
   (an administrator creates them in PwnDoc's user management).

### Day-to-day commands

```bash
./start_all.sh              # start (or re-run safely; it never recreates existing data)
./start_all.sh status       # gateway process and PwnDoc containers
./start_all.sh stop         # stop gateway and PwnDoc; all data is kept
./start_all.sh --help       # every option
tail -f logs/gateway.log    # gateway log
```

Report data lives in the PwnDoc Docker volumes; gateway settings, backups and the RAG index live in
`./data` (see [Configuration](#configuration)). `stop` removes neither. To update, run
`git pull && ./start_all.sh`.

| Variable | Default | Purpose |
|---|---|---|
| `PWNDOC_ADMIN_USER` / `PWNDOC_ADMIN_PASSWORD` | `admin` / `Admin@12345` | First-run administrator (upper, lower, digit, 8+ chars) |
| `GATEWAY_PORT` | `8080` (next free port if busy) | The one port users need; open it in your firewall |
| `PWNDOC_PORT` | `8443` (next free port if busy) | PwnDoc's own port, kept on `127.0.0.1` |
| `GATEWAY_MODE` | `https` | `http` serves plain HTTP (trusted networks only) |
| `PWNDOC_PUBLIC` | `0` | `1` publishes PwnDoc's port on all interfaces too |
| `WITH_RAG` | `0` | `1` installs the local-documents extra (large download) |

### Troubleshooting

| Symptom | Fix |
|---|---|
| `port 8080 is busy, using 8081` | Normal. Open the port shown in the summary, or set `GATEWAY_PORT` |
| `docker: command not found` / daemon not running | Install Docker and start it (`sudo systemctl start docker`) |
| Image pull fails with `docker-credential-desktop not found` | Handled automatically with an anonymous Docker config |
| Warning that the admin login does not work | PwnDoc already had users: use the password you set earlier, or reset it in PwnDoc |
| Browser says the connection is not private | Expected with the self-signed certificate; continue once |
| Need to see what went wrong | `tail -n 100 logs/gateway.log` and `docker compose -p pwndoc logs` in `./pwndoc` |

### Manual install (without `start_all.sh`)

```bash
# 1. Fetch and start upstream PwnDoc (clones a pinned release into ./pwndoc, prebuilt images)
./scripts/setup_pwndoc.sh

# 2. Install and start the gateway, reachable from your LAN
python -m venv .venv && . .venv/bin/activate
pip install -e '.[rag,https]'        # extras are optional, see below
pwndoc-ai serve --host 0.0.0.0 --port 8080 --https
```

Then open `https://<this-machine-ip>:8080`. On a brand-new PwnDoc, its first-run page asks you to
create the administrator yourself.

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
